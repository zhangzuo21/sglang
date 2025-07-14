from __future__ import annotations

"""
Copyright 2023-2025 SGLang Team
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
    http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import concurrent.futures
import logging
import math
import threading
from queue import Empty, Full, PriorityQueue, Queue
from typing import TYPE_CHECKING, List, Optional, Union

import torch

if TYPE_CHECKING:
    from sglang.srt.mem_cache.allocator import BaseTokenToKVPoolAllocator
    from sglang.srt.mem_cache.memory_pool_host import HostKVCache, MLATokenToKVPoolHost

from sglang.srt.distributed import (
    get_tensor_model_parallel_world_size,
    tensor_model_parallel_all_gather,
    get_world_group
)
from sglang.srt.distributed.parallel_state import get_tensor_model_parallel_rank

from sglang.srt.mem_cache.hicache_storage import HiCacheFile, MooncakeStore, get_hash_str, get_hash_str_mooncake

logger = logging.getLogger(__name__)


class LayerDoneCounter:
    def __init__(self, num_layers):
        self.num_layers = num_layers
        # extra producer and consumer counters for overlap mode
        self.num_counters = 3
        self.counters = [num_layers] * self.num_counters
        self.conditions = [threading.Condition() for _ in range(self.num_counters)]
        self.producer_index = 0
        self.consumer_index = 0

    def next_producer(self):
        return (self.producer_index + 1) % self.num_counters

    def update_producer(self):
        self.producer_index = self.next_producer()
        return self.producer_index

    def set_consumer(self, index):
        self.consumer_index = index

    def increment(self):
        with self.conditions[self.producer_index]:
            self.counters[self.producer_index] += 1
            self.conditions[self.producer_index].notify_all()

    def wait_until(self, threshold):
        with self.conditions[self.consumer_index]:
            while self.counters[self.consumer_index] <= threshold:
                self.conditions[self.consumer_index].wait()

    def reset(self):
        with self.conditions[self.producer_index]:
            self.counters[self.producer_index] = 0


class L3LoadCacheOperation:
    counter = 0

    def __init__(
        self,
        device_indices: torch.Tensor,
        data: torch.Tensor,
        node_id: Union[int, List[int]],
        priority: Optional[int] = None,
    ):
        self.device_indices = device_indices
        self.node_ids = [node_id]
        self.data = data

        self.id = CacheOperation.counter
        CacheOperation.counter += 1
        # default priority is the order of creation
        self.priority = priority if priority is not None else self.id

    def merge(self, other: "L3LoadCacheOperation", cat_dim: int = 1) -> None:
        # multiple operations can be merged into a single operation for batch processing
        self.device_indices = torch.cat([self.device_indices, other.device_indices])
        self.priority = min(self.priority, other.priority)
        self.node_ids.extend(other.node_ids)
        self.data = torch.cat([self.data, other.data], dim=cat_dim)

    def __lt__(self, other: "L3LoadCacheOperation"):
        return self.priority < other.priority


class MooncakeStoreCacheOperation:
    counter = 0

    def __init__(
        self,
        mooncake_keys: List,
        host_indices: torch.Tensor,
        node_id: Union[int, List[int]],
        priority: Optional[int] = None,
    ):
        self.host_indices = host_indices
        self.node_ids = [node_id]
        self.mooncake_keys = mooncake_keys

        self.id = CacheOperation.counter
        CacheOperation.counter += 1
        # default priority is the order of creation
        self.priority = priority if priority is not None else self.id

    def merge(self, other: "MooncakeStoreCacheOperation") -> None:
        # multiple operations can be merged into a single operation for batch processing
        self.host_indices = torch.cat([self.host_indices, other.host_indices])
        self.priority = min(self.priority, other.priority)
        self.mooncake_keys.extend(other.mooncake_keys)
        self.node_ids.extend(other.node_ids)

    def __lt__(self, other: "MooncakeStoreCacheOperation"):
        return self.priority < other.priority

# class MooncakeIsBatchExistOperation:
#     counter = 0

#     def __init__(self, l3_keys: list, node_id: Union[int, List[int]]):
#         self.l3_keys = l3_keys
#         self.node_ids = [node_id]
#         self.id = CacheOperation.counter
#         CacheOperation.counter += 1

class CacheOperation:

    counter = 0

    def __init__(
        self,
        host_indices: torch.Tensor,
        device_indices: torch.Tensor,
        node_id: int,
        priority: Optional[int] = None,
        page_size: Optional[int] = None,
    ):
        self.host_indices = host_indices
        self.device_indices = device_indices
        self.node_ids = [node_id]
        self.data = None
        self.page_size = page_size

        self.id = CacheOperation.counter
        CacheOperation.counter += 1
        # default priority is the order of creation
        self.priority = priority if priority is not None else self.id

    def merge(self, other: "CacheOperation") -> None:
        # multiple operations can be merged into a single operation for batch processing
        self.host_indices = torch.cat([self.host_indices, other.host_indices])
        self.device_indices = torch.cat([self.device_indices, other.device_indices])
        self.priority = min(self.priority, other.priority)
        self.node_ids.extend(other.node_ids)

    def split(self, factor) -> List["CacheOperation"]:
        # split an operation into smaller operations to reduce the size of intermediate buffers
        if factor <= 1:
            return [self]

        chunk_size = math.ceil(len(self.host_indices) / factor)
        split_ops = []
        for i in range(0, len(self.host_indices), chunk_size):
            split_ops.append(
                CacheOperation(
                    host_indices=self.host_indices[i : i + chunk_size],
                    device_indices=self.device_indices[i : i + chunk_size],
                    node_id=0,
                    page_size=self.page_size,
                )
            )
        # Inherit the node_ids on the final chunk
        if split_ops:
            split_ops[-1].node_ids = self.node_ids

        return split_ops

    def __lt__(self, other: "CacheOperation"):
        return self.priority < other.priority


class TransferBuffer:
    """
    Overlapping buffer preparation and transfer operations to improve throughput.
    """

    def __init__(
        self, stop_event, buffer_count: int = 3, max_buffer_size: int = 1024
    ) -> None:
        self.stop_event = stop_event
        self.buffers = Queue(maxsize=buffer_count)
        # todo: adjust the buffer size based on throughput profile of the system
        self.max_buffer_size = max_buffer_size

    def full(self) -> bool:
        return self.buffers.full()

    def empty(self) -> bool:
        return self.buffers.empty()

    def put(self, item, block=True, timeout=1) -> None:
        while not self.stop_event.is_set():
            try:
                self.buffers.put(item, block=block, timeout=timeout)
                break
            except Full:
                if not block:
                    break
                continue
            except Exception as e:
                logger.error(e)

    def get(self, block=True, timeout=1) -> Optional[CacheOperation]:
        try:
            return self.buffers.get(block=block, timeout=timeout)
        except Empty:
            return None
        except Exception as e:
            logger.error(e)

    def clear(self):
        self.buffers.queue.clear()

class StorageOperation:
    counter = 0

    def __init__(
        self,
        host_indices: torch.Tensor,
        token_ids: List[int],
        last_hash: Optional[str] = None,
    ):
        self.host_indices = host_indices
        self.token_ids = token_ids
        self.last_hash = last_hash
        self.completed_tokens = 0
        self.hash_value = []

        self.id = StorageOperation.counter
        StorageOperation.counter += 1

    def __lt__(self, other: "StorageOperation"):
        return self.id < other.id


class PrefetchOperation(StorageOperation):
    def __init__(
        self,
        request_id: str,
        host_indices: torch.Tensor,
        token_ids: List[int],
        last_hash: Optional[str] = None,
    ):
        self.request_id = request_id

        self._done_flag = threading.Event()

        super().__init__(host_indices, token_ids, last_hash)

    def mark_done(self):
        self._done_flag.set()

    def is_done(self) -> bool:
        return self._done_flag.is_set()



class HiCacheController:

    def __init__(
        self,
        token_to_kv_pool_allocator: BaseTokenToKVPoolAllocator,
        mem_pool_host: HostKVCache,
        page_size: int,
        # enable_mooncake_store_l3_cache: bool,
        load_cache_event: threading.Event = None,
        write_policy: str = "write_through_selective",
        # mooncake_l3_kv_pool: MooncakeStore = None,
        # mooncake_l3_load_cache_event: threading.Event = None,
        storage_backend: Optional[str] = None,
        prefetch_threshold: int = 256,
    ):
        self.mem_pool_device_allocator = token_to_kv_pool_allocator
        self.mem_pool_device = token_to_kv_pool_allocator.get_kvcache()
        self.mem_pool_host = mem_pool_host
        self.write_policy = write_policy
        self.page_size = page_size
        # using kernel for small page KV cache transfer and DMA for large pages
        # todo: hardware aware config, server parameter
        self.io_backend = "direct" if self.page_size >= 64 else "kernel"

        self.enable_storage = False
        # todo: move backend initialization to storage backend module
        if storage_backend is not None:
            if storage_backend == "file":
                self.storage_backend = HiCacheFile()
                self.enable_storage = True
                # tracking prefetch operation progress
                self.ongoing_prefetch: dict[int, PrefetchOperation] = {}
                # todo: threshold policy for prefetching
                self.prefetch_threshold = prefetch_threshold
                self.storage_zerocopy = False
                self.storage_batchedio = False
            elif storage_backend == "mooncake":
                self.storage_backend = MooncakeStore()
                self.storage_backend.register_buffer(self.mem_pool_host.kv_buffer)
                self.enable_storage = True
                # tracking prefetch operation progress
                self.ongoing_prefetch: dict[int, PrefetchOperation] = {}
                # todo: threshold policy for prefetching
                self.prefetch_threshold = prefetch_threshold
                self.storage_zerocopy = True
                self.storage_batchedio = True
            else:
                raise NotImplementedError(
                    f"Unsupported storage backend: {storage_backend}"
                )
        
        self.load_cache_event = load_cache_event
        self.layer_done_counter = LayerDoneCounter(self.mem_pool_device.layer_num)
        self.mem_pool_device.register_layer_transfer_counter(self.layer_done_counter)

        if write_policy not in [
            "write_through",
            "write_through_selective",
            "write_back",
        ]:
            raise ValueError(f"Invalid write policy: {write_policy}")

        self.write_queue = PriorityQueue()
        self.load_queue = PriorityQueue()

        self.ack_write_queue = Queue()
        self.ack_load_queue = Queue()

        self.stop_event = threading.Event()
        self.write_buffer = TransferBuffer(self.stop_event)
        self.load_buffer = TransferBuffer(
            self.stop_event, buffer_count=10, max_buffer_size=100
        )

        self.write_stream = torch.cuda.Stream()
        self.load_stream = torch.cuda.Stream()

        self.write_thread = threading.Thread(
            target=self.write_thread_func_direct, daemon=True
        )
        self.load_thread = threading.Thread(
            target=self.load_thread_func_layer_by_layer, daemon=True
        )
        self.write_thread.start()
        self.load_thread.start()

        # self.enable_mooncake_store_l3_cache = enable_mooncake_store_l3_cache
        # if self.enable_mooncake_store_l3_cache:

        #     self.mooncake_l3_kv_pool = mooncake_l3_kv_pool

        #     self.mooncake_l3_write_queue = PriorityQueue()
        #     self.mooncake_load_queue = PriorityQueue()
        #     self.l3_load_queue = PriorityQueue()

        #     self.mooncake_l3_stop_event = threading.Event()

        #     self.mooncake_l3_load_cache_event = mooncake_l3_load_cache_event

        #     self.mooncake_l3_ack_load_queue = Queue()

        #     # L2 -> L3
        #     self.mooncake_l3_write_thread = threading.Thread(
        #         target=self.mooncake_l3_write_thread_func_direct,
        #         daemon=True,
        #     )
        #     # L3 -> L2
        #     self.mooncake_load_thread = threading.Thread(
        #         target=self.mooncake_load_thread_func, daemon=True
        #     )

        #     self.mooncake_l3_write_thread.start()
        #     self.mooncake_load_thread.start()
        if self.enable_storage:
            self.prefetch_thread = threading.Thread(
                target=self.prefetch_thread_func, daemon=True
            )
            self.backup_thread = threading.Thread(
                target=self.backup_thread_func, daemon=True
            )
            self.prefetch_queue = Queue()
            self.backup_queue = Queue()

            self.prefetch_revoke_queue = Queue()
            self.ack_backup_queue = Queue()

            self.prefetch_thread.start()
            self.backup_thread.start()

    def reset(self):
        self.stop_event.set()
        self.write_thread.join()
        self.load_thread.join()

        self.write_queue.queue.clear()
        self.load_queue.queue.clear()
        self.write_buffer.clear()
        self.load_buffer.clear()
        self.ack_write_queue.queue.clear()
        self.ack_load_queue.queue.clear()
        if self.enable_storage:
            self.prefetch_thread.join()
            self.backup_thread.join()
            self.prefetch_queue.queue.clear()
            self.backup_queue.queue.clear()
            self.prefetch_revoke_queue.queue.clear()
            self.ack_backup_queue.queue.clear()

        self.write_thread = threading.Thread(
            target=self.write_thread_func_direct, daemon=True
        )
        self.load_thread = threading.Thread(
            target=self.load_thread_func_layer_by_layer, daemon=True
        )
        self.stop_event.clear()
        self.write_thread.start()
        self.load_thread.start()

        # if self.enable_mooncake_store_l3_cache:
        #     self.mooncake_l3_stop_event.set()
        #     self.mooncake_l3_write_thread.join()
        #     self.mooncake_load_thread.join()

        #     self.mooncake_l3_write_queue.queue.clear()
        #     self.mooncake_load_queue.queue.clear()
        #     self.l3_load_queue.queue.clear()

        #     self.mooncake_l3_ack_load_queue.queue.clear()

        #     self.mooncake_l3_write_thread = threading.Thread(
        #         target=self.mooncake_l3_write_thread_func_direct,
        #         daemon=True,
        #     )
        #     self.mooncake_load_thread = threading.Thread(
        #         target=self.mooncake_load_thread_func, daemon=True
        #     )

        #     self.mooncake_l3_stop_event.clear()

        #     self.mooncake_l3_write_thread.start()
        #     self.mooncake_load_thread.start()
        if self.enable_storage:
            self.prefetch_thread = threading.Thread(
                target=self.prefetch_thread_func, daemon=True
            )
            self.backup_thread = threading.Thread(
                target=self.backup_thread_func, daemon=True
            )
            self.prefetch_thread.start()
            self.backup_thread.start()

    def write(
        self,
        device_indices: torch.Tensor,
        priority: Optional[int] = None,
        node_id: int = 0,
    ) -> Optional[torch.Tensor]:
        """
        Back up KV caches from device memory to host memory.
        """
        host_indices = self.mem_pool_host.alloc(len(device_indices))
        if host_indices is None:
            return None
        self.mem_pool_host.protect_write(host_indices)
        self.write_queue.put(
            CacheOperation(
                host_indices,
                device_indices,
                node_id,
                priority,
                page_size=self.page_size,
            )
        )
        return host_indices

    def load(
        self,
        host_indices: torch.Tensor,
        priority: Optional[int] = None,
        node_id: int = 0,
    ) -> Optional[torch.Tensor]:
        """
        Load KV caches from host memory to device memory.
        """
        device_indices = self.mem_pool_device_allocator.alloc(len(host_indices))
        if device_indices is None:
            return None
        self.mem_pool_host.protect_load(host_indices)
        # to ensure the device indices are ready before accessed by another CUDA stream
        torch.cuda.current_stream().synchronize()
        self.load_queue.put(
            CacheOperation(host_indices, device_indices, node_id, priority)
        )
        return device_indices

    def move_indices(self, host_indices, device_indices):
        if self.io_backend == "kernel":
            return host_indices.to(self.mem_pool_device.device), device_indices
        elif self.io_backend == "direct":
            return host_indices, device_indices.cpu()
        else:
            raise ValueError(f"Unsupported io backend")

    def mooncake_load(
        self,
        l3_keys: List[str],
        slots_required: int,
        priority: Optional[int] = None,
        node_id: Union[int, List[int]] = 0,
    ) -> Optional[torch.Tensor]:
        host_indices = self.mem_pool_host.alloc(slots_required)
        if host_indices is None:
            return None
        self.mooncake_load_queue.put(
            MooncakeStoreCacheOperation(l3_keys, host_indices, node_id, priority=priority)
        )
        return host_indices

    def write_thread_func_direct(self):
        """
        Directly write through KV caches to host memory without buffering.
        """
        torch.cuda.set_stream(self.write_stream)
        while not self.stop_event.is_set():
            try:
                operation = self.write_queue.get(block=True, timeout=1)
                host_indices, device_indices = self.move_indices(
                    operation.host_indices, operation.device_indices
                )
                self.mem_pool_device.backup_to_host_all_layer(
                    self.mem_pool_host,
                    host_indices,
                    device_indices,
                    self.io_backend,
                )
                self.write_stream.synchronize()
                self.mem_pool_host.complete_io(operation.host_indices)

                # write L3 cache
                # if self.enable_mooncake_store_l3_cache:
                #     mooncake_operation = MooncakeStoreCacheOperation(
                #         operation.l3_keys,
                #         operation.host_indices,
                #         operation.node_ids,
                #         priority=operation.priority,
                #     )

                #     self.mooncake_l3_write_queue.put(mooncake_operation)

                for node_id in operation.node_ids:
                    if node_id != 0:
                        self.ack_write_queue.put(node_id)
            except Empty:
                continue
            except Exception as e:
                logger.error(e)

    def mooncake_l3_write_thread_func_direct(self):
        while not self.mooncake_l3_stop_event.is_set():
            try:
                operation = self.mooncake_l3_write_queue.get(block=True, timeout=0.001)
                keys = operation.mooncake_keys
                mooncake_exist_keys = self.mooncake_l3_kv_pool.is_batch_exist(
                    keys
                )
                key_strs, buffer_ptrs, buffer_sizes = self.mem_pool_host.get_buffer_meta(operation.mooncake_keys,
                                                                                 operation.host_indices)
                self.mooncake_l3_kv_pool.batch_put(key_strs, buffer_ptrs, buffer_sizes)

            except Empty:
                continue
            except Exception as e:
                logger.error(e)


    # def mooncake_load_thread_func(self):
    #     while not self.mooncake_l3_stop_event.is_set():
    #         try:
    #             operation = self.mooncake_load_queue.get(block=True, timeout=0.001)
    #             if isinstance(operation, MooncakeStoreCacheOperation):
    #                 key_strs, buffer_ptrs, buffer_sizes = self.mem_pool_host.get_buffer_meta(operation.mooncake_keys,
    #                                                                                  operation.host_indices)
    #                 self.mooncake_l3_kv_pool.batch_get(key_strs, buffer_ptrs, buffer_sizes)

    #                 for node_id in operation.node_ids:
    #                     if node_id != 0:
    #                         self.mooncake_l3_ack_load_queue.put(node_id)
    #             elif isinstance(operation, MooncakeIsBatchExistOperation):
    #                 self.mooncake_l3_kv_pool.is_batch_exist(operation.l3_keys)
    #                 for node_id in operation.node_ids:
    #                     if node_id != 0:
    #                         self.mooncake_l3_ack_load_queue.put(node_id)

    #         except Empty:
    #             continue
    #         except Exception as e:
    #             logger.error(e)

    def load_thread_func_layer_by_layer(self):
        """
        Load KV caches from host memory to device memory layer by layer.
        """
        torch.cuda.set_stream(self.load_stream)
        while not self.stop_event.is_set():
            self.load_cache_event.wait(timeout=1)
            if not self.load_cache_event.is_set():
                continue
            self.load_cache_event.clear()
            self.layer_done_counter.update_producer()

            batch_operation = None
            while self.load_queue.qsize() > 0:
                op = self.load_queue.get(block=True)
                if batch_operation is None:
                    batch_operation = op
                else:
                    batch_operation.merge(op)
            if batch_operation is None:
                continue

            # start layer-wise KV cache transfer from CPU to GPU
            self.layer_done_counter.reset()

            host_indices, device_indices = self.move_indices(
                batch_operation.host_indices, batch_operation.device_indices
            )
            for i in range(self.mem_pool_host.layer_num):
                self.mem_pool_device.load_from_host_per_layer(
                    self.mem_pool_host,
                    host_indices,
                    device_indices,
                    i,
                    self.io_backend,
                )
                self.load_stream.synchronize()
                self.layer_done_counter.increment()

            self.mem_pool_host.complete_io(batch_operation.host_indices)
            for node_id in batch_operation.node_ids:
                if node_id != 0:
                    self.ack_load_queue.put(node_id)

    def evict_device(
        self, device_indices: torch.Tensor, host_indices: torch.Tensor
    ) -> int:
        if self.mem_pool_host.is_synced(host_indices):
            self.mem_pool_device_allocator.free(device_indices)
            self.mem_pool_host.update_backup(host_indices)
            return len(device_indices)
        else:
            raise ValueError(
                f"Inconsistent states: {self.mem_pool_host.get_state(host_indices)}"
            )

    def evict_host(self, host_indices: torch.Tensor, backup_only: bool = True) -> int:
        if not backup_only:
            raise ValueError("Other eviction policies are not supported yet.")

        if self.mem_pool_host.is_backup(host_indices):
            self.mem_pool_host.free(host_indices)
            return len(host_indices)
        else:
            raise ValueError(
                f"Inconsistent states: {self.mem_pool_host.get_state(host_indices)}"
            )
    def prefetch(
        self,
        request_id: str,
        host_indices: torch.Tensor,
        new_input_tokens: List[int],
        last_hash: Optional[str] = None,
    ) -> int:
        """
        Prefetch KV caches from storage backend to host memory.
        """
        operation = PrefetchOperation(
            request_id, host_indices, new_input_tokens, last_hash
        )
        self.ongoing_prefetch[request_id] = operation
        self.prefetch_queue.put(operation)

    def terminate_prefetch(self, request_id: str):
        operation = self.ongoing_prefetch.pop(request_id, None)
        if operation is None:
            raise ValueError(
                f"Request ID {request_id} not found in ongoing prefetches."
            )
        operation.mark_done()
        return operation.completed_tokens, operation.hash_value

    def prefetch_io_aux_func(self):
        """
        Auxiliary function conducting IO operations for prefetching.
        """
        while not self.stop_event.is_set():
            try:
                operation = self.prefetch_buffer.get(block=True, timeout=1)
                if self.storage_batchedio:
                    if self.storage_zerocopy:

                        key_strs, buffer_ptrs, buffer_sizes = self.mem_pool_host.get_buffer_meta(operation.hash_value[:-1],
                                                                                     operation.host_indices[:-self.page_size])
                        self.storage_backend.get(key_strs, buffer_ptrs, buffer_sizes)

                    else:
                        pass
                    operation.completed_tokens += len(operation.hash_value) * self.page_size
                else:
                    for h in operation.hash_value:
                        if self.storage_zerocopy:
                            #unimplemented
                            pass
                        else:
                            page_data = self.storage_backend.get(h)
                            if page_data is None:
                                logger.warning(
                                    f"Prefetch operation {operation.request_id} failed to retrieve page {h}."
                                )
                                break
                            self.mem_pool_host.set_from_flat_data_page(
                                operation.host_indices[operation.completed_tokens],
                                page_data,
                            )
                        if operation.is_done():
                            # operation terminated by controller, release pre-allocated memory
                            self.mem_pool_host.free(
                                operation.host_indices[operation.completed_tokens :]
                            )
                            break
                        operation.completed_tokens += self.page_size
            except Empty:
                continue

    def prefetch_thread_func(self):
        """
        Manage prefetching operations from storage backend to host memory.
        """
        self.prefetch_buffer = Queue()
        aux_thread = threading.Thread(target=self.prefetch_io_aux_func, daemon=True)
        aux_thread.start()
        while (not self.stop_event.is_set()) or not self.prefetch_queue.empty():
            try:
                operation = self.prefetch_queue.get(block=True, timeout=1)
                if operation is None:
                    continue

                last_hash = operation.last_hash
                tokens_to_fetch = operation.token_ids

                storage_hit_count = 0
                remaining_tokens = len(tokens_to_fetch)
                hash_value = []
                while remaining_tokens >= self.page_size:
                    if isinstance(self.storage_backend, HiCacheFile):
                        last_hash = get_hash_str(
                            tokens_to_fetch[
                                storage_hit_count : storage_hit_count + self.page_size
                            ],
                            last_hash,
                        )
                    elif isinstance(self.storage_backend, MooncakeStore):
                        local_rank = torch.cuda.current_device()
                        last_hash = get_hash_str_mooncake(
                                last_hash, 
                                tokens_to_fetch[
                                    storage_hit_count : storage_hit_count + self.page_size
                                ],
                                local_rank
                            )
                    if not self.storage_batchedio:
                        exist_result = self.storage_backend.exists(last_hash)
                    if (not self.storage_batchedio) and exist_result:
                        storage_hit_count += self.page_size
                        hash_value.append(last_hash)
                        remaining_tokens -= self.page_size
                    elif self.storage_batchedio:
                        storage_hit_count += self.page_size
                        hash_value.append(last_hash)
                        remaining_tokens -= self.page_size
                    else:
                        break
                if self.storage_batchedio:
                    exist_result = self.storage_backend.exists(hash_value)
                    storage_hit_count = sum(1 for v in exist_result.values() if v != 0) * self.page_size

                if storage_hit_count < self.prefetch_threshold:
                    logger.info("not enough for prefetching")
                    # not to prefetch if not enough benefits
                    self.prefetch_revoke_queue.put(operation.request_id)
                else:
                    operation.hash_value = hash_value
                    logger.info(
                        f"Prefetching {len(hash_value)} pages for request {operation.request_id}."
                    )
                    self.prefetch_buffer.put(operation)

            except Empty:
                continue

    def write_storage(
        self,
        host_indices: torch.Tensor,
        token_ids: List[int],
        last_hash: Optional[str] = None,
    ) -> int:
        """
        Write KV caches from host memory to storage backend.
        """
        operation = StorageOperation(host_indices, token_ids, last_hash)
        self.backup_queue.put(operation)
        return operation.id

    def backup_thread_func(self):
        """
        Manage backup operations from host memory to storage backend.
        """
        while not self.stop_event.is_set():
            try:
                operation = self.backup_queue.get(block=True, timeout=1)
                if operation is None:
                    continue

                last_hash = operation.last_hash
                tokens_to_backup = operation.token_ids

                backup_hit_count = 0
                remaining_tokens = len(tokens_to_backup)
                hash_value = []
                while remaining_tokens >= self.page_size:
                    if isinstance(self.storage_backend, HiCacheFile):
                        last_hash = get_hash_str(
                            tokens_to_backup[
                                backup_hit_count : backup_hit_count + self.page_size
                            ],
                            last_hash,
                        )
                    elif isinstance(self.storage_backend, MooncakeStore):
                        local_rank = torch.cuda.current_device()
                        last_hash = get_hash_str_mooncake(
                                last_hash, 
                                tokens_to_backup[
                                    backup_hit_count : backup_hit_count + self.page_size
                                ],
                                local_rank
                            )
                    backup_hit_count += self.page_size
                    hash_value.append(last_hash)
                    remaining_tokens -= self.page_size
                    operation.hash_value = hash_value

                if self.storage_batchedio:
                    if self.storage_zerocopy:
                        exist_hashvalues = self.storage_backend.exists(hash_value)
                        indices = operation.host_indices.tolist()
                        non_exist_keys = []
                        non_exist_indices = []
                        for i in range(len(hash_value)):
                            if not exist_hashvalues[hash_value[i]]:
                                non_exist_keys.append(hash_value[i])
                                non_exist_indices.extend(indices[i * self.page_size: (i + 1) * self.page_size])
                        if len(non_exist_keys) > 0:
                            key_strs, buffer_ptrs, buffer_sizes = self.mem_pool_host.get_buffer_meta(non_exist_keys,
                                                                                             non_exist_indices)
                            self.storage_backend.set(key_strs, target_location=buffer_ptrs, target_sizes=buffer_sizes)

                        operation.comleted_tokens += len(hash_value) * self.page_size
                    else:
                        #unimplemented
                        pass
                else:
                    for i in range(0, len(tokens_to_backup), self.page_size):
                        last_hash = hash_value[i]
                        # todo, handle failures in storage backend
                        if self.storage_zerocopy:
                            pass
                        else:
                            self.storage_backend.set(
                                last_hash,
                                self.mem_pool_host.get_flat_data_page(
                                    operation.host_indices[i]
                                ),
                            )
                        operation.completed_tokens += self.page_size

                self.ack_backup_queue.put((operation.id, operation.hash_value))

            except Empty:
                continue