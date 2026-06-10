"""Moments like business rules."""

from datetime import timedelta
import random


def plan_next_moments_like_time(now, *, min_minutes, max_minutes, randint=None, log_info=None):
    """Plan the next random Moments like time."""
    randint = randint or random.randint
    log_info = log_info or (lambda message: None)
    low = max(1, min_minutes)
    high = max(low, max_minutes)
    delay_min = randint(low, high)
    next_time = now + timedelta(minutes=delay_min)
    log_info(f"随机朋友圈点赞：下次触发 {next_time.strftime('%H:%M:%S')}（{delay_min} 分钟后）")
    return next_time


def perform_moments_like(
    *,
    open_moments,
    sleep,
    random_delay,
    notify_error,
    nickname,
    log_info=None,
    log_warning=None,
    log_error=None,
):
    """Perform one random Moments like interaction using injected runtime operations."""
    log_info = log_info or (lambda message: None)
    log_warning = log_warning or (lambda message: None)
    log_error = log_error or (lambda message: None)
    moments_view = None

    log_info("随机朋友圈点赞：开始执行...")
    try:
        moments_view = open_moments()
        if moments_view is None:
            log_error("随机点赞：打开朋友圈失败（返回None），请在手机端确认朋友圈功能已开启")
            notify_error(f"{nickname} wxbot随机朋友圈点赞失败！", "打开朋友圈返回None")
            return

        sleep(random_delay(1, 5))

        moments = moments_view.GetMoments()
        if not moments:
            log_warning("随机点赞：获取朋友圈内容为空，跳过本次点赞")
            sleep(random_delay(1, 5))
            moments_view.Close()
            return

        sleep(random_delay(1, 5))

        moments[0].Like()
        log_info("随机朋友圈点赞：点赞完成")

        sleep(random_delay(1, 5))
        moments_view.Close()
        log_info("随机朋友圈点赞：朋友圈已关闭")
    except Exception as exc:
        log_error(f"随机朋友圈点赞执行出错：{exc}")
        notify_error(f"{nickname} wxbot随机朋友圈点赞失败！", exc)
        try:
            if moments_view is not None:
                moments_view.Close()
        except Exception:
            pass


def execute_moments_like_task(*, task, perform_like, log_info=None, log_error=None):
    """Execute one concrete Moments like task that is already due."""
    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)
    task = task if isinstance(task, dict) else {}
    task_id = str(task.get("id") or task.get("task_id") or "moments-like").strip() or "moments-like"
    log_info(f"随机朋友圈点赞任务 {task_id}：开始执行...")
    try:
        perform_like()
        return True
    except Exception as exc:
        log_error(f"随机朋友圈点赞任务 {task_id} 执行失败：{exc}")
        return False
