"""Retry utilities for iflow CLI operations.

Ported from feishu-iflow-bridge/src/modules/IFlowAdapter.js executeWithRetry().
Original author: Wuguoshuo

Provides exponential backoff retry logic that can wrap any async callable.
"""

import asyncio
from typing import Any, Callable, Coroutine, Optional, TypeVar

from loguru import logger


T = TypeVar("T")


class RetryExhaustedError(Exception):
    """Raised when all retry attempts are exhausted."""
    def __init__(self, message: str, last_error: Optional[Exception] = None):
        super().__init__(message)
        self.last_error = last_error


async def with_retry(
    func: Callable[..., Coroutine[Any, Any, T]],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    retry_on: tuple[type[Exception], ...] = (Exception,),
    operation_name: str = "operation",
    **kwargs: Any,
) -> T:
    """Execute an async function with exponential backoff retry.

    Args:
        func: Async function to execute
        *args: Positional arguments for func
        max_retries: Maximum number of retry attempts (total calls = max_retries)
        base_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay between retries (seconds)
        backoff_factor: Multiplier for delay after each retry
        retry_on: Tuple of exception types that should trigger retry
        operation_name: Human-readable name for logging
        **kwargs: Keyword arguments for func

    Returns:
        Return value of func

    Raises:
        RetryExhaustedError: When all retries are exhausted
    """
    last_error: Optional[Exception] = None
    delay = base_delay

    for attempt in range(1, max_retries + 1):
        try:
            result = await func(*args, **kwargs)
            if attempt > 1:
                logger.info(f"{operation_name} succeeded on attempt {attempt}/{max_retries}")
            return result

        except retry_on as e:
            last_error = e
            if attempt == max_retries:
                logger.error(
                    f"{operation_name} failed after {max_retries} attempts: {e}"
                )
                break

            logger.warning(
                f"{operation_name} failed (attempt {attempt}/{max_retries}): {e}. "
                f"Retrying in {delay:.1f}s..."
            )
            await asyncio.sleep(delay)
            delay = min(delay * backoff_factor, max_delay)

    raise RetryExhaustedError(
        f"{operation_name} failed after {max_retries} attempts",
        last_error=last_error,
    )


async def chat_with_retry(
    adapter: Any,
    message: str,
    channel: str = "cli",
    chat_id: str = "direct",
    model: Optional[str] = None,
    timeout: Optional[int] = None,
    max_retries: int = 3,
) -> str:
    """Convenience wrapper: call adapter.chat() with retry.

    Args:
        adapter: IFlowAdapter instance
        message: Message to send
        channel: Channel name
        chat_id: Chat ID
        model: Model name override
        timeout: Timeout override
        max_retries: Maximum retry attempts

    Returns:
        Response text from iflow
    """
    return await with_retry(
        adapter.chat,
        message=message,
        channel=channel,
        chat_id=chat_id,
        model=model,
        timeout=timeout,
        max_retries=max_retries,
        operation_name=f"chat({channel}:{chat_id})",
    )
