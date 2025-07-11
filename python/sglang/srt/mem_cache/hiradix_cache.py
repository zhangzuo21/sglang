import hashlib
import heapq
import logging
import threading
import time
from typing import List, Optional

import numpy as np
import torch

from sglang.srt.managers.cache_controller import HiCacheController
from sglang.srt.mem_cache.allocator import BaseTokenToKVPoolAllocator
from sglang.srt.mem_cache.base_prefix_cache import MatchResult
from sglang.srt.managers.schedule_batch import Req, WaitingStatus
from sglang.srt.mem_cache.memory_pool import (
    MHATokenToKVPool,
    MLATokenToKVPool,
    ReqToTokenPool,
)
from sglang.srt.mem_cache.memory_pool_host import (
    MHATokenToKVPoolHost,
    MLATokenToKVPoolHost,
)
from sglang.srt.mem_cache.mooncake_store import MooncakeStore
from sglang.srt.mem_cache.radix_cache import RadixCache, TreeNode

logger = logging.getLogger(__name__)


def page_token_ids_to_key(
    prefix_block_key: str, current_page_ids: List, local_rank: int
):
    prefix_str = ""
    if len(prefix_block_key):
        prefix_str = hashlib.sha256(prefix_block_key.encode()).hexdigest()
    current_token_ids_bytes = np.array(current_page_ids).tobytes()
    current_hash_object = hashlib.sha256(current_token_ids_bytes)
    current_hash_hex = current_hash_object.hexdigest()
    return f"{prefix_str}_{int(current_hash_hex[:16], 16)}_{local_rank}"


def get_node_l3_keys(
    token_ids: List,
    current_token_len: int,
    prefix_block_key: str = "",
    local_rank: int = 0,
    page_size: int = 1,
):
    l3_keys = []
    total_block_len = len(token_ids) // page_size
    current_block_len = current_token_len // page_size
    for i in range(total_block_len - current_block_len, total_block_len):
        current_block_token_ids = token_ids[i * page_size : (i + 1) * page_size]
        current_block_key = page_token_ids_to_key(
            prefix_block_key, current_block_token_ids, local_rank
        )
        l3_keys.append(current_block_key)
        prefix_block_key = current_block_key

    return l3_keys


class HiRadixCache(RadixCache):

    def __init__(
        self,
        req_to_token_pool: ReqToTokenPool,
        token_to_kv_pool_allocator: BaseTokenToKVPoolAllocator,
        tp_cache_group: torch.distributed.ProcessGroup,
        page_size: int,
        hicache_ratio: float,
        hicache_size: int,
        hicache_write_policy: str,
        hicache_storage_backend: Optional[str] = None,
    ):
        self.kv_cache = token_to_kv_pool_allocator.get_kvcache()
        if isinstance(self.kv_cache, MHATokenToKVPool):
            self.token_to_kv_pool_host = MHATokenToKVPoolHost(
                self.kv_cache, hicache_ratio, hicache_size, page_size
            )
        elif isinstance(self.kv_cache, MLATokenToKVPool):
            self.token_to_kv_pool_host = MLATokenToKVPoolHost(
                self.kv_cache, hicache_ratio, hicache_size, page_size
            )
        else:
            raise ValueError(f"HiRadixCache only supports MHA and MLA yet")

        self.mooncake_l3_kv_pool = None
        self.mooncake_l3_load_cache_event = None
        self.page_size = page_size
        if hicache_storage_backend == "mooncake":
            # TODO(huangtingwei9988):L3 cache only support write_through_selective and write_through write policy
            assert hicache_write_policy in ["write_through_selective", "write_through"]
            self.mooncake_l3_kv_pool = MooncakeStore()
            self.mooncake_l3_kv_pool.register_buffer(self.token_to_kv_pool_host.kv_buffer)
            self.mooncake_l3_load_cache_event = threading.Event()
            self.l3_ongoing_load_back = {}

        self.tp_group = tp_cache_group
        self.enable_storage = hicache_storage_backend is not None
        # todo: customizable storage prefetch threshold
        self.prefetch_threshold = 256

        self.load_cache_event = threading.Event()
        self.cache_controller = HiCacheController(
            token_to_kv_pool_allocator,
            self.token_to_kv_pool_host,
            page_size,
            enable_mooncake_store_l3_cache,
            load_cache_event=self.load_cache_event,
            write_policy=hicache_write_policy,
            mooncake_l3_kv_pool=self.mooncake_l3_kv_pool,
            mooncake_l3_load_cache_event=self.mooncake_l3_load_cache_event,
        )

        # record the nodes with ongoing write through
        self.ongoing_write_through = {}
        # record the node segments with ongoing load back
        self.ongoing_load_back = {}
        # record the ongoing prefetch requests
        self.outstanding_prefetch = {}
        self.ongoing_backup = {}
        # todo: dynamically adjust the threshold
        self.write_through_threshold = (
            1 if hicache_write_policy == "write_through" else 3
        )
        self.write_through_threshold_storage = 3
        self.load_back_threshold = 10
        super().__init__(
            req_to_token_pool, token_to_kv_pool_allocator, page_size, disable=False
        )

    def reset(self):
        TreeNode.counter = 0
        self.cache_controller.reset()
        self.token_to_kv_pool_host.clear()
        super().reset()

    def get_height(self, node: TreeNode):
        height = 0
        while node != self.root_node:
            node = node.parent
            height += 1
        return height

    # def write_backup(
    #     self, node: TreeNode, write_back=False, token_ids: Optional[List] = None
    # ):
    #     l3_keys = []
    #     if self.enable_mooncake_store_l3_cache:
    #         # The KV cache of each rank in the MLA model is the same, so only one copy needs to be stored
    #         local_rank =  torch.cuda.current_device()
    #         prefix_block_key = (
    #             ""
    #             if node.parent is None or len(node.parent.l3_keys) == 0
    #             else node.parent.l3_keys[-1]
    #         )
    #         l3_keys = get_node_l3_keys(
    #             token_ids, len(node.value), prefix_block_key, local_rank, self.page_size
    #         )

    #     host_indices = self.cache_controller.write(
    #         device_indices=node.value,
    #         node_id=node.id,
    #         l3_keys=l3_keys if self.enable_mooncake_store_l3_cache else None,
    #     )
    #     if host_indices is None:
    #         self.evict_host(len(node.value))
    #         host_indices = self.cache_controller.write(
    #             device_indices=node.value,
    #             node_id=node.id,
    #             l3_keys=l3_keys if self.enable_mooncake_store_l3_cache else None,
    #         )
    #     if host_indices is not None:
    #         node.host_value = host_indices
    #         self.ongoing_write_through[node.id] = node
    #         if not write_back:
    #             # no need to lock nodes if write back
    #             self.inc_lock_ref(node)
    #     else:
    #         return 0

    #     if len(l3_keys) > 0:
    #         node.l3_keys = l3_keys

    #     return len(host_indices)
    
    def write_backup(self, node: TreeNode, write_back=False):
        host_indices = self.cache_controller.write(
            device_indices=node.value,
            node_id=node.id,
        )
        if host_indices is None:
            self.evict_host(len(node.value))
            host_indices = self.cache_controller.write(
                device_indices=node.value,
                node_id=node.id,
            )
        if host_indices is not None:
            node.host_value = host_indices
            self.ongoing_write_through[node.id] = node
            if not write_back:
                # no need to lock nodes if write back
                self.inc_lock_ref(node)
        else:
            return 0

        return len(host_indices)

    def write_backup_storage(self, node: TreeNode):
        operation_id = self.cache_controller.write_storage(
            node.host_value, node.key, node.parent.get_last_hash_value()
        )
        self.ongoing_backup[operation_id] = node
        node.protect_host()

    def inc_hit_count(self, node: TreeNode, token_ids: Optional[List] = None):
        if node.backuped or self.cache_controller.write_policy == "write_back":
            return
        node.hit_count += 1
        # if node.hit_count >= self.write_through_threshold:
        #     self.write_backup(node, token_ids=token_ids)
        #     node.hit_count = 0

        if not node.backuped:
            if node.hit_count >= self.write_through_threshold:
                # write to host if the node is not backuped
                self.write_backup(node)
        else:
            if (
                self.enable_storage
                and (not node.backuped_storage)
                and node.hit_count >= self.write_through_threshold_storage
            ):
                # if the node is backuped on host memory but not on storage
                self.write_backup_storage(node)

    def writing_check(self, write_back=False):
        if write_back:
            # blocking till all write back complete
            while len(self.ongoing_write_through) > 0:
                ack_id = self.cache_controller.ack_write_queue.get()
                del self.ongoing_write_through[ack_id]
            return
        queue_size = torch.tensor(
            self.cache_controller.ack_write_queue.qsize(), dtype=torch.int
        )
        if torch.distributed.get_world_size(group=self.tp_group) > 1:
            # synchrnoize TP workers to make the same update to radix cache
            torch.distributed.all_reduce(
                queue_size,
                op=torch.distributed.ReduceOp.MIN,
                group=self.tp_group,
            )
        for _ in range(queue_size.item()):
            ack_id = self.cache_controller.ack_write_queue.get()
            self.dec_lock_ref(self.ongoing_write_through[ack_id])
            del self.ongoing_write_through[ack_id]

    def waiting_status_check(self, req: Req):
        if torch.distributed.get_world_size(group=self.tp_group) > 1:
            check_ready = torch.tensor([req.waiting_status == WaitingStatus.READY])
            torch.distributed.all_reduce(
                check_ready,
                op=torch.distributed.ReduceOp.MIN,
                group=self.tp_group,
            )
            if not check_ready.item():
                return False
        else:
            if req.waiting_status != WaitingStatus.READY:
                return False

        return True

    def l3_loading_check(self):
        while not self.cache_controller.mooncake_l3_ack_load_queue.empty():
            try:
                ack_id = self.cache_controller.mooncake_l3_ack_load_queue.get_nowait()
                start_node, end_node, req = self.l3_ongoing_load_back[ack_id]
                self.dec_lock_ref(end_node)
                while end_node != start_node:
                    assert end_node.loading
                    end_node.loading = False
                    end_node = end_node.parent
                else:
                    req.waiting_status = WaitingStatus.READY

                # clear the reference
                del self.l3_ongoing_load_back[ack_id]
                req.last_node = req.last_l3_node
                req.host_hit_length += req.l3_hit_length
            except Exception:
                break

    def loading_check(self):
        while not self.cache_controller.ack_load_queue.empty():
            try:
                ack_id = self.cache_controller.ack_load_queue.get_nowait()
                start_node, end_node = self.ongoing_load_back[ack_id]
                self.dec_lock_ref(end_node)
                while end_node != start_node:
                    assert end_node.loading
                    end_node.loading = False
                    end_node = end_node.parent
                # clear the reference
                del self.ongoing_load_back[ack_id]
            except Exception:
                break

    def evictable_size(self):
        return self.evictable_size_

    def evict(self, num_tokens: int):
        leaves = self._collect_leaves_device()
        heapq.heapify(leaves)

        num_evicted = 0
        write_back_nodes = []
        while num_evicted < num_tokens and len(leaves):
            x = heapq.heappop(leaves)

            if x.lock_ref > 0:
                continue

            if not x.backuped:
                if self.cache_controller.write_policy == "write_back":
                    # write to host if the node is not backuped
                    num_evicted += self.write_backup(x, write_back=True)
                    write_back_nodes.append(x)
                else:
                    num_evicted += self._evict_regular(x)
            else:
                num_evicted += self._evict_backuped(x)

            for child in x.parent.children.values():
                if child in write_back_nodes:
                    continue
                if not child.evicted:
                    break
            else:
                # all children are evicted or no children
                heapq.heappush(leaves, x.parent)

        if self.cache_controller.write_policy == "write_back":
            self.writing_check(write_back=True)
            for node in write_back_nodes:
                assert node.backuped
                self._evict_backuped(node)

    def _evict_backuped(self, node: TreeNode):
        # evict a node already written to host
        num_evicted = self.cache_controller.evict_device(node.value, node.host_value)
        assert num_evicted > 0
        self.evictable_size_ -= num_evicted
        node.value = None
        return num_evicted

    def _evict_regular(self, node: TreeNode):
        # evict a node not initiated write to host
        self.cache_controller.mem_pool_device_allocator.free(node.value)
        num_evicted = len(node.value)
        self._delete_leaf(node)
        return num_evicted

    def evict_host(self, num_tokens: int):
        leaves = self._collect_leaves()
        heapq.heapify(leaves)

        num_evicted = 0
        while num_evicted < num_tokens and len(leaves):
            x = heapq.heappop(leaves)
            if x == self.root_node:
                break
            # only evict the host value of evicted nodes
            if not x.evicted:
                continue

            # node is protected from eviction as it has ongoing prefetch or backup to storage
            if x.host_ref_counter > 0:
                continue

            num_evicted += self.cache_controller.evict_host(x.host_value)

            for k, v in x.parent.children.items():
                if v == x:
                    break
            if len(x.parent.children[k].l3_keys) == 0:
                del x.parent.children[k]

            if len(x.parent.children) == 0 and x.parent.evicted:
                heapq.heappush(leaves, x.parent)

    def mooncake_load_back(self, req: Req, node: TreeNode):
        last_hit_node = node
        if last_hit_node.id in self.l3_ongoing_load_back.keys():
            return

        l3_nodes_to_load = []
        while node.evicted and not node.l2_backuped and node.l3_backuped:
            l3_nodes_to_load.insert(0, node)
            node = node.parent
        else:
            ancester_node = node

        self.inc_lock_ref(ancester_node)

        l3_keys = [key for n in l3_nodes_to_load for key in n.l3_keys]
        slots_required = len(l3_keys) * self.page_size
        host_indices=self.cache_controller.mooncake_load(l3_keys, slots_required, node_id=last_hit_node.id)
        if host_indices is None:
            self.evict_host(slots_required)
            host_indices = self.cache_controller.mooncake_load(l3_keys, slots_required, node_id=last_hit_node.id)
        self.dec_lock_ref(ancester_node)
        if host_indices is None:
            req.waiting_status = WaitingStatus.READY
            return

        self.l3_ongoing_load_back[last_hit_node.id] = (
            ancester_node,
            last_hit_node,
            req,
        )
        offset = 0
        for node in l3_nodes_to_load:
            node.host_value = host_indices[
                offset : offset + len(node.l3_keys) * self.page_size
            ]
        for node in l3_nodes_to_load:
            node.loading = True

    def load_back(
        self, node: TreeNode, mem_quota: Optional[int] = None
    ) -> Optional[torch.Tensor]:
        # todo: more loading policies

        last_hit_node = node
        nodes_to_load = []
        while node.evicted:
            assert (
                node.l2_backuped
            ), "No backup available on evicted nodes, should not happen"
            nodes_to_load.insert(0, node)
            node = node.parent
        else:
            ancester_node = node

        # protect the ancestor nodes from eviction
        delta = self.inc_lock_ref(ancester_node)

        # load it all or not at all
        host_indices = torch.cat([n.host_value for n in nodes_to_load])
        if len(host_indices) < self.load_back_threshold or (
            len(host_indices) > mem_quota + delta if mem_quota is not None else False
        ):
            # skip loading back if the total size is too small or exceeding the memory quota
            self.dec_lock_ref(ancester_node)
            return None

        device_indices = self.cache_controller.load(
            host_indices=host_indices, node_id=last_hit_node.id
        )
        if device_indices is None:
            self.evict(len(host_indices))
            device_indices = self.cache_controller.load(
                host_indices=host_indices, node_id=last_hit_node.id
            )
        self.dec_lock_ref(ancester_node)
        if device_indices is None:
            # no sufficient GPU memory to load back KV caches
            return None

        self.ongoing_load_back[last_hit_node.id] = (ancester_node, last_hit_node)
        offset = 0
        for node in nodes_to_load:
            node.value = device_indices[offset : offset + len(node.host_value)]
            offset += len(node.host_value)
            node.loading = True
        self.evictable_size_ += len(device_indices)
        self.inc_lock_ref(last_hit_node)
        return device_indices

    def init_load_back(
        self,
        last_node: TreeNode,
        host_hit_length: int,
        mem_quota: Optional[int] = None,
    ):
        _ = host_hit_length  # unused, but kept for compatibility
        if last_node.evicted:
            loading_values = self.load_back(last_node, mem_quota)
            if loading_values is not None:
                logger.debug(
                    f"loading back {len(loading_values)} tokens for node {last_node.id}"
                )
                return loading_values, last_node

            while last_node.evicted:
                last_node = last_node.parent

        return (
            torch.empty((0,), dtype=torch.int64, device=self.device),
            last_node,
        )

    def ready_to_load_host_cache(self):
        producer_index = self.cache_controller.layer_done_counter.next_producer()
        self.load_cache_event.set()
        if self.mooncake_l3_load_cache_event:
            self.mooncake_l3_load_cache_event.set()
        return producer_index

    def check_hicache_events(self):
        self.writing_check()
        self.loading_check()
        if self.enable_mooncake_store_l3_cache:
            self.l3_loading_check()

    def match_prefix(self, key: List[int], do_prefetch=False, **kwargs):
        empty_value = torch.empty((0,), dtype=torch.int64, device=self.device)
        if self.disable or len(key) == 0:
            return MatchResult(
                device_indices=empty_value,
                last_device_node=self.root_node,
                last_host_node=self.root_node,
                host_hit_length=0,
            )

        if self.page_size != 1:
            page_aligned_len = len(key) // self.page_size * self.page_size
            key = key[:page_aligned_len]

        value, last_node = self._match_prefix_helper(self.root_node, key, do_prefetch)
        if value:
            value = torch.cat(value)
        else:
            value = empty_value

        last_l3_node = None
        l3_hit_length = 0
        if self.enable_mooncake_store_l3_cache:
            while last_node.evicted and last_node.l3_backuped and not last_node.l2_backuped:
                if not last_l3_node:
                    last_l3_node = last_node
                l3_hit_length += len(last_node.l3_keys) * self.page_size
                last_node = last_node.parent

        host_hit_length = 0
        last_host_node = last_node
        while last_node.evicted and last_node.l2_backuped:
            host_hit_length += len(last_node.host_value)
            last_node = last_node.parent

        return MatchResult(
            device_indices=value,
            last_device_node=last_node,
            last_host_node=last_host_node,
            host_hit_length=host_hit_length,
            last_l3_node=last_l3_node,
            l3_hit_length=l3_hit_length
        )

    def _match_prefix_helper(self, node: TreeNode, key: List, do_prefetch=False):
        total_key = key
        node.last_access_time = time.monotonic()
        child_key = self.get_child_key_fn(key)
        value = []
        total_prefix_length = 0

        while len(key) > 0 and child_key in node.children.keys():
            child = node.children[child_key]
            child.last_access_time = time.monotonic()
            prefix_len = self.key_match_fn(child.key, key)
            total_prefix_length += prefix_len
            if prefix_len < len(child.key):
                new_node = self._split_node(child.key, child, prefix_len)
                self.inc_hit_count(new_node, token_ids=total_key[:total_prefix_length])
                if not new_node.evicted:
                    value.append(new_node.value)
                node = new_node
                key = key[prefix_len:]
                break
            else:
                self.inc_hit_count(child, token_ids=total_key[:total_prefix_length])
                if not child.evicted:
                    value.append(child.value)
                node = child
                key = key[prefix_len:]

                if len(key):
                    child_key = self.get_child_key_fn(key)

        if self.enable_mooncake_store_l3_cache and do_prefetch:
            prefetch_begin = time.time()
            # try to get the cross instance shared kv cache
            if len(key) and (not node.evicted or node.backuped):
                local_rank =  torch.cuda.current_device()
                prefix_block_key = (
                    ""
                    if node.parent is None or len(node.parent.l3_keys) == 0
                    else node.parent.l3_keys[-1]
                )
                l3_keys = get_node_l3_keys(
                    total_key, len(key), prefix_block_key, local_rank, self.page_size
                )
                mooncake_exist_keys = self.cache_controller.is_batch_exist(l3_keys, node.id)
                try:
                    ack_id = self.cache_controller.mooncake_l3_ack_load_queue.get(timeout=self.prefetch_threshold)
                    is_batch_exist_end = time.time()
                except TimeoutError:
                    return value, node
                l3_exist_keys = []
                for l3_key in l3_keys:
                    if mooncake_exist_keys[l3_key]:
                        l3_exist_keys.append(l3_key)
                    else:
                        break

                if len(l3_exist_keys) > 0:
                    child_key = self.get_child_key_fn(
                        key[: len(l3_exist_keys) * self.page_size]
                    )
                    new_node = TreeNode()
                    new_node.parent = node
                    new_node.key = key[: len(l3_exist_keys) * self.page_size]
                    node.children[child_key] = new_node
                    new_node.l3_keys = l3_exist_keys
                    node = new_node
            #TODO L2 lock
            time_left = self.prefetch_threshold if not is_batch_exist_end else self.prefetch_threshold - (is_batch_exist_end - prefetch_begin)
            nodes_to_prefetch: list[TreeNode] = []
            while node.evicted and node.l2_backuped:
                if node.l3_backuped:
                    nodes_to_prefetch.insert(0, node)

            l3_keys = [key for node in nodes_to_prefetch for key in node.l3_keys]
            slots_required = len(l3_keys) * self.page_size
            nodes_id = [node.id for node in nodes_to_prefetch]
            self.cache_controller.mooncake_load(l3_keys, slots_required, node_id=nodes_id)
            try:
                first_id = self.cache_controller.mooncake_l3_ack_load_queue.get(timeout=time_left)
                while not self.cache_controller.mooncake_l3_ack_load_queue.empty():
                    self.cache_controller.mooncake_l3_ack_load_queue.get()

            except TimeoutError:
                #TODO: halt message
                pass

        return value, node

    def _split_node(self, key, child: TreeNode, split_len: int):
        # child node split into new_node -> child
        new_node = TreeNode()
        new_node.children = {self.get_child_key_fn(key[split_len:]): child}
        new_node.parent = child.parent
        new_node.lock_ref = child.lock_ref
        new_node.key = child.key[:split_len]
        new_node.loading = child.loading
        new_node.hit_count = child.hit_count

        # split value and host value if exists
        if child.evicted:
            new_node.value = None
        else:
            new_node.value = child.value[:split_len]
            child.value = child.value[split_len:]
        if child.l2_backuped:
            new_node.host_value = child.host_value[:split_len]
            child.host_value = child.host_value[split_len:]
        if child.l3_backuped:
            new_node.l3_keys = child.l3_keys[: split_len // self.page_size]
            child.l3_keys = child.l3_keys[split_len // self.page_size :]
        child.parent = new_node
        child.key = child.key[split_len:]
        new_node.parent.children[self.get_child_key_fn(key)] = new_node
        return new_node

    def _insert_helper(self, node: TreeNode, key: List, value):
        total_key = key
        node.last_access_time = time.monotonic()
        if len(key) == 0:
            return 0

        child_key = self.get_child_key_fn(key)
        total_prefix_length = 0

        while len(key) > 0 and child_key in node.children.keys():
            node = node.children[child_key]
            node.last_access_time = time.monotonic()
            prefix_len = self.key_match_fn(node.key, key)

            if prefix_len == len(node.key):
                if node.evicted:
                    # change the reference if the node is evicted
                    # this often happens in the case of KV cache recomputation
                    node.value = value[:prefix_len]
                    self.token_to_kv_pool_host.update_synced(node.host_value)
                    self.evictable_size_ += len(node.value)
                else:
                    total_prefix_length += prefix_len
                    self.inc_hit_count(node, token_ids=total_key[:total_prefix_length])
            else:
                # partial match, split the node
                new_node = self._split_node(node.key, node, prefix_len)
                if new_node.evicted:
                    new_node.value = value[:prefix_len]
                    self.token_to_kv_pool_host.update_synced(new_node.host_value)
                    self.evictable_size_ += len(new_node.value)
                else:
                    total_prefix_length += prefix_len
                    self.inc_hit_count(
                        new_node, token_ids=total_key[:total_prefix_length]
                    )
                node = new_node

            key = key[prefix_len:]
            value = value[prefix_len:]

            if len(key):
                child_key = self.get_child_key_fn(key)

        if len(key):
            new_node = TreeNode()
            new_node.parent = node
            new_node.key = key
            new_node.value = value
            node.children[child_key] = new_node
            self.evictable_size_ += len(value)

            if self.cache_controller.write_policy != "write_back":
                self.inc_hit_count(new_node, token_ids=total_key)
        return total_prefix_length

    def _collect_leaves_device(self):
        def is_leaf(node):
            if node.evicted:
                return False
            if node == self.root_node:
                return False
            if len(node.children) == 0:
                return True
            for child in node.children.values():
                if not child.evicted:
                    return False
            return True

        ret_list = []
        stack = [self.root_node]
        while stack:
            cur_node = stack.pop()
            if is_leaf(cur_node):
                ret_list.append(cur_node)
            else:
                for cur_child in cur_node.children.values():
                    if not cur_child.evicted:
                        stack.append(cur_child)
        return ret_list
