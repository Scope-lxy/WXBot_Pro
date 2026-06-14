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
    except Exception:
        pass
    try:
        items = moments.GetMoments(force_wait=1)
    except TypeError:
        try:
            items = moments.GetMoments()
        except Exception as exc:
            return None, f"读取朋友圈列表失败：{exc}"
    except Exception as exc:
        return None, f"读取朋友圈列表失败：{exc}"
    if isinstance(items, list):
        return items, ""
    try:
        return list(items or []), ""
    except Exception as exc:
        return None, f"朋友圈列表结果无法解析：{exc}"


def _verify_moments_editor_closed(moments, *, log_info=None, log_error=None):
    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)
    items, reason = _load_visible_moments(moments, log_info=log_info, log_error=log_error)
    if items is None:
        return False, [], reason or "发布后未能确认已退出编辑器"
    return True, items, ""


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
        return True, ""

    if visible_moments is not None:
        items, reason = visible_moments, ""
    else:
        items, reason = _load_visible_moments(
            moments,
            log_info=log_info,
            log_error=log_error,
        )
    if items is None:
        return False, reason or "发布后未通过内容校验"

    subset = list(items[:max_items])
    for index, moment in enumerate(subset, start=1):
        content = _moment_content_text(moment)
        if not content:
            continue
        for token in tokens:
            if token and token in content:
                return True, ""
    return False, "发布后未通过内容校验"


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

    moments = open_moments()
    if moments is None:
        message = "打开朋友圈失败，请确认微信已开启朋友圈功能"
        log_error(f"朋友圈发布失败：{message}")
        notify_error(
            f"{nickname} wxbot朋友圈发布失败！",
            "打开朋友圈失败，请在手机端确认朋友圈功能已开启",
        )
        return False

    delay1 = random_delay(2, 5)
    sleep(delay1)

    try:
        moments.Publish(text, valid_images if valid_images else None, privacy_config)
        delay_verify = random_delay(9, 12)
        sleep(delay_verify)

        editor_closed, visible_moments, editor_reason = _verify_moments_editor_closed(
            moments,
            log_info=log_info,
            log_error=log_error,
        )
        if not editor_closed:
            log_error(f"朋友圈发布失败：{editor_reason}")
            notify_error(
                f"{nickname} wxbot朋友圈发布失败！",
                "朋友圈发布后未能确认已退出编辑器，请检查是否有其他微信自动化流程打断",
            )
            return False

        weak_verified, verify_reason = _verify_latest_moment_content(
            moments,
            text=text,
            visible_moments=visible_moments,
            log_info=log_info,
            log_error=log_error,
        )
        if not weak_verified:
            log_error(f"朋友圈发布失败：{verify_reason}")
            notify_error(
                f"{nickname} wxbot朋友圈发布失败！",
                "朋友圈发布后未通过内容校验，请检查朋友圈首条内容是否与本次文案一致",
            )
            return False

        preview = text[:30] + "..." if len(text) > 30 else text
        log_info(f"朋友圈发布成功：内容：{preview}，图片数：{len(valid_images)}")
        return True
    finally:
        delay2 = random_delay(2, 5)
        sleep(delay2)
        moments.Close()


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

    log_info("朋友圈发布开始")
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
        log_error(f"朋友圈发布失败：{exc}")
        notify_error(
            f"{nickname} wxbot朋友圈发布失败！",
            f"朋友圈任务发送失败：{exc}",
        )
    return False
