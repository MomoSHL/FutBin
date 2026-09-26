"""
Task orchestration service with queue-based triggers and collision prevention.
Manages price checking and dashboard update coordination.
"""

import asyncio
import logging
import time
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict, Any, Callable, Awaitable


class TaskType(Enum):
    """Types of tasks that can be queued"""
    PRICE_CHECK = "price_check"
    DASHBOARD_UPDATE = "dashboard_update"
    FORCE_PRICE_CHECK = "force_price_check"
    SINGLE_PLAYER_CHECK = "single_player_check"
    USER_DASHBOARD_UPDATE = "user_dashboard_update"


import itertools

_task_seq = itertools.count()

@dataclass
class TaskRequest:
    """Task request with metadata"""
    task_type: TaskType
    priority: int = 0  # Higher = more priority
    data: Optional[Dict[str, Any]] = None
    requested_at: float = None
    seq: int = 0
    
    def __post_init__(self):
        if self.requested_at is None:
            self.requested_at = time.time()
        self.seq = next(_task_seq)
    
    def __lt__(self, other):
        """Compare tasks for priority queue ordering"""
        if not hasattr(other, 'priority') or not hasattr(other, 'requested_at'):
            return NotImplemented
        # Higher priority value = higher priority (reverse comparison)
        if self.priority != other.priority:
            return self.priority > other.priority
        # If priorities are equal, use requested_at (FIFO)
        if self.requested_at != other.requested_at:
            return self.requested_at < other.requested_at
        # Tie-breaker
        return self.seq < getattr(other, 'seq', 0)


class TaskOrchestrator:
    """Orchestrates bot tasks with queue-based coordination"""
    
    def __init__(self, max_queue_size: int = 100):
        self._task_queue = asyncio.PriorityQueue(maxsize=max_queue_size)
        self._running_tasks: Dict[TaskType, asyncio.Task] = {}
        self._task_handlers: Dict[TaskType, Callable[[TaskRequest], Awaitable[None]]] = {}
        
        self._is_running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._logger = logging.getLogger(__name__)
        
        # Task execution locks to prevent collisions
        self._task_locks: Dict[TaskType, asyncio.Lock] = {
            TaskType.PRICE_CHECK: asyncio.Lock(),
            TaskType.DASHBOARD_UPDATE: asyncio.Lock(),
            TaskType.FORCE_PRICE_CHECK: asyncio.Lock(),
            TaskType.SINGLE_PLAYER_CHECK: asyncio.Lock(),
            TaskType.USER_DASHBOARD_UPDATE: asyncio.Lock(),
        }
        
        # Statistics
        self.stats = {
            'tasks_executed': 0,
            'tasks_failed': 0,
            'queue_overflows': 0,
            'last_execution': {},
        }

    def register_handler(self, task_type: TaskType, handler: Callable[[TaskRequest], Awaitable[None]]):
        """Register a handler for a specific task type"""
        self._task_handlers[task_type] = handler
        self._logger.info(f"Registered handler for {task_type.value}")

    async def start(self):
        """Start the task orchestrator"""
        if self._is_running:
            return
            
        self._is_running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._logger.info("Task orchestrator started")

    async def stop(self):
        """Stop the task orchestrator"""
        if not self._is_running:
            return
            
        self._is_running = False
        
        # Cancel running tasks
        for task in list(self._running_tasks.values()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Cancel worker
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        
        self._logger.info("Task orchestrator stopped")

    async def trigger_task(self, task_type: TaskType, priority: int = 0, data: Optional[Dict[str, Any]] = None) -> bool:
        """Trigger a task with given priority"""
        if not self._is_running:
            self._logger.warning(f"Cannot trigger {task_type.value}: orchestrator not running")
            return False
        
        request = TaskRequest(task_type=task_type, priority=priority, data=data)
        
        try:
            # Put TaskRequest directly - it has __lt__ for comparison
            await self._task_queue.put(request)
            self._logger.debug(f"Queued task: {task_type.value} (priority: {priority})")
            return True
        except asyncio.QueueFull:
            self.stats['queue_overflows'] += 1
            self._logger.warning(f"Task queue full, dropping {task_type.value}")
            return False

    async def trigger_price_check(self, force: bool = False, player_urls: Optional[list] = None) -> bool:
        """Trigger a price check task"""
        task_type = TaskType.FORCE_PRICE_CHECK if force else TaskType.PRICE_CHECK
        priority = 10 if force else 5
        
        data = {}
        if player_urls:
            data['player_urls'] = player_urls
            task_type = TaskType.SINGLE_PLAYER_CHECK
            priority = 15  # Higher priority for single player checks
        
        return await self.trigger_task(task_type, priority, data)

    async def trigger_dashboard_update(self, force: bool = False) -> bool:
        """Trigger a dashboard update task"""
        priority = 8 if force else 3
        return await self.trigger_task(TaskType.DASHBOARD_UPDATE, priority)

    async def trigger_user_dashboard_update(self, dashboard_name: str, owner_id: int, force: bool = False) -> bool:
        """Trigger a user dashboard update task"""
        priority = 8 if force else 3
        data = {
            'dashboard_name': dashboard_name,
            'owner_id': owner_id
        }
        return await self.trigger_task(TaskType.USER_DASHBOARD_UPDATE, priority, data)

    def is_task_running(self, task_type: TaskType) -> bool:
        """Check if a specific task type is currently running"""
        task = self._running_tasks.get(task_type)
        return task is not None and not task.done()

    def get_queue_size(self) -> int:
        """Get current queue size"""
        return self._task_queue.qsize()

    def get_stats(self) -> Dict[str, Any]:
        """Get orchestrator statistics"""
        running_tasks = [t.value for t, task in self._running_tasks.items() if not task.done()]
        
        return {
            **self.stats,
            'queue_size': self.get_queue_size(),
            'running_tasks': running_tasks,
            'is_running': self._is_running,
        }

    async def _worker_loop(self):
        """Main worker loop that processes queued tasks"""
        self._logger.info("Task orchestrator worker loop started")
        
        while self._is_running:
            try:
                # Get next task from queue (with timeout to allow clean shutdown)
                try:
                    request = await asyncio.wait_for(
                        self._task_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                # Check if we have a handler for this task type
                handler = self._task_handlers.get(request.task_type)
                if not handler:
                    self._logger.warning(f"No handler registered for {request.task_type.value}")
                    continue
                
                # Check if this task type is already running
                if self.is_task_running(request.task_type):
                    self._logger.debug(f"Task {request.task_type.value} already running, skipping")
                    continue
                
                # Execute the task
                await self._execute_task(request, handler)
                
            except Exception as e:
                self._logger.error(f"Error in worker loop: {e}")
                await asyncio.sleep(1)  # Prevent tight error loop

    async def _execute_task(self, request: TaskRequest, handler: Callable[[TaskRequest], Awaitable[None]]):
        """Execute a task with proper locking and error handling"""
        task_type = request.task_type
        
        # Get the appropriate lock
        lock = self._task_locks.get(task_type)
        if not lock:
            self._logger.error(f"No lock available for task type: {task_type.value}")
            return
        
        # Try to acquire lock (non-blocking)
        if lock.locked():
            self._logger.debug(f"Task {task_type.value} is locked, skipping")
            return
        
        async with lock:
            try:
                self._logger.debug(f"Starting task: {task_type.value}")
                start_time = time.time()
                
                # Create and store the task
                task = asyncio.create_task(handler(request))
                self._running_tasks[task_type] = task
                
                # Execute the task
                await task
                
                # Update statistics
                execution_time = time.time() - start_time
                self.stats['tasks_executed'] += 1
                self.stats['last_execution'][task_type.value] = {
                    'timestamp': time.time(),
                    'duration': execution_time,
                    'success': True
                }
                
                self._logger.debug(f"Completed task: {task_type.value} in {execution_time:.2f}s")
                
            except asyncio.CancelledError:
                self._logger.info(f"Task cancelled: {task_type.value}")
                self.stats['last_execution'][task_type.value] = {
                    'timestamp': time.time(),
                    'duration': time.time() - start_time,
                    'success': False,
                    'error': 'cancelled'
                }
                
            except Exception as e:
                self._logger.error(f"Task failed: {task_type.value} - {e}")
                self.stats['tasks_failed'] += 1
                self.stats['last_execution'][task_type.value] = {
                    'timestamp': time.time(),
                    'duration': time.time() - start_time,
                    'success': False,
                    'error': str(e)
                }
                
            finally:
                # Clean up the running task reference
                self._running_tasks.pop(task_type, None)