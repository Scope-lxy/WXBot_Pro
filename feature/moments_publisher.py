"""Low-level Moments publishing adapter for wxautoX4."""


def _normalize_moment_text(text):
    text = str(text or "").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]
    return " ".join(part for part in lines if part)


def _build_publish_match_tokens(text):
    normalized = _normalize_moment_text(text)
    if not normalized:
        return []
    tokens = [normalized if len(normalized) <= 12 else normalized[:12]]
    if len(normalized) > 12:
        tail = normalized[-8:]
        if tail not in tokens:
            tokens.append(tail)
    return [token for token in tokens if token]


def _load_visible_moments(moments, *, log_info=None, log_error=None):
    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)
    try:
        moments.Refresh()
    except Exception as exc:
        log_info(f"朋友圈列表刷新失败，继续尝试读取当前列表：{exc}")
    try:
        items = moments.GetMoments(force_wait=1)
    except TypeError:
        items = moments.GetMoments()
    except Exception as exc:
        log_error(f"读取朋友圈列表失败：{exc}")
        return None
    if isinstance(items, list):
        return items
    try:
        return list(items or [])
    except Exception as exc:
        log_error(f"朋友圈列表结果无法解析：{exc}")
        return None


def _verify_moments_editor_closed(moments, *, log_info=None, log_error=None):
    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)
    log_info("朋友圈发布第一层校验：检查是否已退出编辑器")
    items = _load_visible_moments(moments, log_info=log_info, log_error=log_error)
    if items is None:
        log_error("朋友圈发布第一层校验失败：未能回到可读取的朋友圈列表")
        return False, []
    log_info("朋友圈发布第一层校验通过：已退出编辑器")
    return True, items


def _moment_content_text(moment):
    try:
        content = getattr(moment, "content", "")
    except Exception:
        return ""
    return _normalize_moment_text(content)


def _verify_latest_moment_content(moments, *, text, visible_moments=None, max_items=3, log_info=None, log_error=None):
    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)
    tokens = _build_publish_match_tokens(text)
    if not tokens:
        log_info("朋友圈发布第二层校验跳过：本次无文本文案，按编辑器退出结果判定")
        return True

    items = visible_moments if visible_moments is not None else _load_visible_moments(
        moments,
        log_info=log_info,
        log_error=log_error,
    )
    if items is None:
        log_error("朋友圈发布第二层校验失败：无法读取朋友圈列表内容")
        return False

    subset = list(items[:max_items])
    log_info(f"朋友圈发布第二层校验：检测前 {len(subset)} 条朋友圈内容")
    for index, moment in enumerate(subset, start=1):
        content = _moment_content_text(moment)
        if not content:
            continue
        for token in tokens:
            if token and token in content:
                log_info(f"朋友圈发布第二层校验命中：第 {index} 条包含片段「{token}」")
                return True
    log_error("朋友圈发布第二层校验失败：未命中本次文案特征")
    return False


def build_moments_privacy_config(privacy, tags):
    if privacy == "whitelist":
        return {"privacy": "白名单", "tags": tags}
    if privacy == "blacklist":
        return {"privacy": "黑名单", "tags": tags}
    return {}


def publish_moments_post(
    *,
    text,
    images,
    privacy,
    tags,
    open_moments,
    sleep,
    random_delay,
    notify_error,
    nickname,
    log_info=None,
    log_error=None,
):
    """Publish one Moments post using injected wxautoX4 operations."""
    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)
    privacy_config = build_moments_privacy_config(privacy, tags)
    valid_images = [img for img in images or [] if img and img.strip()]

    log_info("正在打开朋友圈...")
    moments = open_moments()
    if moments is None:
        message = "打开朋友圈失败（返回None），请确认微信已开启朋友圈功能"
        log_error(message)
        notify_error(
            f"{nickname} wxbot朋友圈发布失败！",
            "打开朋友圈失败，请在手机端确认朋友圈功能已开启",
        )
        return False

    delay1 = random_delay(2, 5)
    log_info(f"朋友圈已打开，等待 {delay1:.1f}s 后发布...")
    sleep(delay1)

    try:
        moments.Publish(text, valid_images if valid_images else None, privacy_config)
        log_info("朋友圈发布提交完成，开始校验编辑器退出")
        delay_verify = random_delay(9, 12)
        log_info(f"等待 {delay_verify:.1f}s 后开始发后校验...")
        sleep(delay_verify)

        editor_closed, visible_moments = _verify_moments_editor_closed(
            moments,
            log_info=log_info,
            log_error=log_error,
        )
        if not editor_closed:
            notify_error(
                f"{nickname} wxbot朋友圈发布失败！",
                "朋友圈发布后未能确认已退出编辑器，请检查是否有其他微信自动化流程打断",
            )
            return False

        weak_verified = _verify_latest_moment_content(
            moments,
            text=text,
            visible_moments=visible_moments,
            log_info=log_info,
            log_error=log_error,
        )
        if not weak_verified:
            notify_error(
                f"{nickname} wxbot朋友圈发布失败！",
                "朋友圈发布后未通过内容校验，请检查朋友圈首条内容是否与本次文案一致",
            )
            return False

        preview = text[:30] + "..." if len(text) > 30 else text
        log_info(f"朋友圈已发布，内容：{preview}，图片数：{len(valid_images)}")
        return True
    finally:
        delay2 = random_delay(2, 5)
        log_info(f"等待 {delay2:.1f}s 后关闭朋友圈...")
        sleep(delay2)
        moments.Close()
        log_info("朋友圈已关闭")


def execute_moments_publish_task(
    *,
    task,
    open_moments,
    sleep,
    random_delay,
    notify_error,
    nickname,
    log_info=None,
    log_error=None,
):
    """Execute one concrete Moments publish task that is already due."""
    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)
    task = task if isinstance(task, dict) else {}

    log_info("朋友圈任务时间到，正在发送...")
    try:
        return bool(publish_moments_post(
            text=task.get("text", ""),
            images=task.get("images", []),
            privacy=task.get("privacy", "public"),
            tags=task.get("tags", []),
            open_moments=open_moments,
            sleep=sleep,
            random_delay=random_delay,
            notify_error=notify_error,
            nickname=nickname,
            log_info=log_info,
            log_error=log_error,
        ))
    except Exception as exc:
        log_error(f"朋友圈任务发送失败：{exc}")
        notify_error(
            f"{nickname} wxbot朋友圈发布失败！",
            f"朋友圈任务发送失败：{exc}",
        )
    return False
