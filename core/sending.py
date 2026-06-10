"""Pure reply preparation helpers used before sending WeChat messages."""

import re

DEFAULT_BLOCKED_POLICY = "fallback"
SPLIT_SOURCE_NONE = ""
SPLIT_SOURCE_NEWLINE = "newline"
SPLIT_SOURCE_SENTENCE = "sentence"
SPLIT_SOURCE_SPACE = "space"

THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
LEADING_THINK_RE = re.compile(r"^\s*<think\b[^>]*>", re.IGNORECASE)
LEADING_TIMESTAMP_RE = re.compile(r"^\s*\[\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\]\s*")
LEADING_REPLY_LABEL_RE = re.compile(
    r"^\s*(?:回复|回答|assistant|self|ai|机器人|小东|瑞瑞|弟弟|姐姐|reply)\s*[:：]\s*",
    re.IGNORECASE,
)
META_REPLY_MARKERS = (
    re.compile(r"作为\s*(?:一个)?\s*ai\b", re.IGNORECASE),
    re.compile(r"as\s+an?\s+ai\b", re.IGNORECASE),
    re.compile(r"\bthe\s+request\s+was\s+rejected\b", re.IGNORECASE),
    re.compile(r"\b(?:considered|classified|flagged)\s+(?:as\s+)?high\s+risk\b", re.IGNORECASE),
    re.compile(r"这可能会?引起[^。！？!?]{0,40}(?:误导|风险|伤害|不适|违规|违反)", re.IGNORECASE),
)
META_RESTRICTION_RE = re.compile(
    r"(?:不能|无法|不可以|没办法|不能继续|无法继续|不能满足|无法满足|不能扮演|无法扮演|不能冒充|无法冒充|不会协助|不应当)",
    re.IGNORECASE,
)
META_SUBJECT_RE = re.compile(
    r"(?:ai|人工智能|语言模型|大模型|模型|机器人|系统|平台|政策|规则|规范|安全|合规|伦理|请求|扮演|冒充|真实人物|虚构人物|"
    r"language\s+model|policy|policies|guideline|guidelines|safety|roleplay|impersonat)",
    re.IGNORECASE,
)
META_TASK_RE = re.compile(
    r"(?:帮你|帮助你|生成|提供|协助|处理|满足|回复|回答|请求|内容)",
    re.IGNORECASE,
)
META_SELF_DISCLOSURE_RE = re.compile(
    r"(?:我(?:只是|是)[^。！？!?]{0,20}(?:ai|人工智能|语言模型|大模型|模型|机器人)|"
    r"i\s*(?:am|'m)\s+(?:just\s+)?(?:an?\s+)?(?:ai|language\s+model))",
    re.IGNORECASE,
)
STAGE_DIRECTION_WRAPPER_RE = re.compile(r"^\s*(?:[（(](.*)[）)]|【(.*)】|\[(.*)\]|「(.*)」|『(.*)』|\*(.*)\*)\s*$")
STAGE_DIRECTION_NARRATION_PUNCT_RE = re.compile(r"[，,、；;：:]")
INLINE_STAGE_DIRECTION_RE = re.compile(
    r"(?P<span>[（(](?P<paren>[^()\n]{1,80})[）)]|【(?P<bracket>[^【】\n]{1,80})】|\[(?P<square>[^\[\]\n]{1,80})\]|「(?P<corner>[^「」\n]{1,80})」|『(?P<double_corner>[^『』\n]{1,80})』|\*(?P<star>[^*\n]{1,80})\*)"
)
PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")
SENTENCE_TOKEN_RE = re.compile(r"[^。！？!?]+[。！？!?]?")
HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
STRUCTURED_TEXT_RE = re.compile(
    r"(```|https?://|\b[A-Za-z]:\\|/Users/|^\s*\|.+\|\s*$|^\s*\d+[.)、]\s+|^\s*[-*+]\s+)",
    re.IGNORECASE | re.MULTILINE,
)
SENTENCE_BOUNDARY_RE = re.compile(r"[^。！？!?～~]+[。！？!?～~]?")
COMMA_BOUNDARY_RE = re.compile(r"[^，,；;]+[，,；;]?")
CHINESE_SPACE_REPLY_RE = re.compile(r"^[\u4e00-\u9fff\s，。！？!?；;：:、～~…]+$")
PLAIN_TERMINAL_PUNCTUATION = "，,。.;；、"
AUTO_SPLIT_MIN_LINES = 2
AUTO_SPLIT_MAX_LINE_CHARS = 20
INLINE_STAGE_DIRECTION_HINTS = (
    "声音",
    "声线",
    "嗓音",
    "语气",
    "语调",
    "口吻",
    "低声",
    "轻声",
    "小声",
    "压低",
    "低沉",
    "温和",
    "温柔",
    "认真",
    "刚醒",
    "沙哑",
    "喉咙",
    "呼吸",
    "停顿",
    "沉默",
    "轻笑",
    "失笑",
    "苦笑",
    "叹气",
    "安抚",
    "哄",
    "心疼",
    "慢慢地",
    "轻轻地",
)
SHORT_INLINE_STAGE_DIRECTION_HINTS = (
    "笑",
)


def _looks_like_meta_reply(text):
    snippet = str(text or "").strip()
    if not snippet:
        return False
    if any(pattern.search(snippet) for pattern in META_REPLY_MARKERS):
        return True
    if META_SELF_DISCLOSURE_RE.search(snippet):
        return True
    return bool(META_RESTRICTION_RE.search(snippet) and (META_SUBJECT_RE.search(snippet) or META_TASK_RE.search(snippet)))


def _looks_like_stage_direction_line(text):
    line = str(text or "").strip()
    if not line or len(line) > 100:
        return False
    match = STAGE_DIRECTION_WRAPPER_RE.match(line)
    if not match:
        return False
    inner = next((part for part in match.groups() if part is not None), "").strip()
    if not inner:
        return False
    return len(inner) >= 4 or bool(STAGE_DIRECTION_NARRATION_PUNCT_RE.search(inner))


def _looks_like_inline_stage_direction_text(text):
    snippet = str(text or "").strip()
    if not snippet or len(snippet) > 80:
        return False
    if snippet in SHORT_INLINE_STAGE_DIRECTION_HINTS:
        return True
    return any(hint in snippet for hint in INLINE_STAGE_DIRECTION_HINTS)


def _wrapped_marker_has_neighboring_reply_text(match, full_text):
    source = str(full_text or "")
    prefix = source[:match.start("span")].rstrip()
    suffix = source[match.end("span"):].lstrip()
    return bool(
        re.search(r"[\w\u4e00-\u9fff]", prefix)
        or re.search(r"[\w\u4e00-\u9fff]", suffix)
    )


def _strip_inline_stage_direction_spans(text):
    source = str(text or "")

    def _replace(match):
        inner = next(
            (
                part
                for part in (
                    match.group("paren"),
                    match.group("bracket"),
                    match.group("square"),
                    match.group("corner"),
                    match.group("double_corner"),
                    match.group("star"),
                )
                if part is not None
            ),
            "",
        ).strip()
        if not (
            _looks_like_inline_stage_direction_text(inner)
            or _wrapped_marker_has_neighboring_reply_text(match, source)
        ):
            return match.group(0)
        return ""

    return INLINE_STAGE_DIRECTION_RE.sub(_replace, source)


def _strip_stage_direction_lines(text):
    lines = []
    for line in str(text or "").splitlines():
        if _looks_like_stage_direction_line(line):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _strip_meta_reply_artifacts(text):
    raw = str(text or "").strip()
    if not raw:
        return ""

    paragraphs = [part.strip() for part in PARAGRAPH_SPLIT_RE.split(raw) if part.strip()]
    if not paragraphs:
        return ""

    kept_paragraphs = [part for part in paragraphs if not _looks_like_meta_reply(part)]
    if kept_paragraphs and len(kept_paragraphs) != len(paragraphs):
        return "\n\n".join(kept_paragraphs).strip()

    segments = [match.group(0).strip() for match in SENTENCE_TOKEN_RE.finditer(raw) if match.group(0).strip()]
    if not segments:
        return ""

    kept_segments = []
    dropping_leading_meta = True
    removed_segment = False
    for segment in segments:
        if dropping_leading_meta and _looks_like_meta_reply(segment):
            removed_segment = True
            continue
        dropping_leading_meta = False
        kept_segments.append(segment)
    if not removed_segment:
        return raw
    return "".join(kept_segments).strip()


def sanitize_ai_output_text(text):
    """Remove protocol noise that is safe to strip across all AI output types."""
    if text is None:
        return ""
    cleaned = str(text).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = THINK_BLOCK_RE.sub("", cleaned)

    # Treat a leading unclosed <think> block as unsafe output.
    if LEADING_THINK_RE.search(cleaned):
        tail_match = re.search(r"\n\s*\n", cleaned)
        if tail_match:
            cleaned = cleaned[tail_match.end():]
        else:
            cleaned = ""
    return cleaned


def clean_ai_reply_text(text):
    """Remove model-side artifacts that should never be sent to WeChat."""
    if text is None:
        return ""
    working = str(text)
    working = HTML_BREAK_RE.sub("\n", working)
    cleaned = THINK_BLOCK_RE.sub("", working)
    removed_think = cleaned != working

    # Treat a leading unclosed <think> block as unsafe output.
    if LEADING_THINK_RE.search(cleaned):
        tail_match = re.search(r"\n\s*\n", cleaned)
        if tail_match:
            cleaned = cleaned[tail_match.end():]
        else:
            cleaned = ""

    cleaned = LEADING_TIMESTAMP_RE.sub("", cleaned, count=1)
    cleaned = LEADING_REPLY_LABEL_RE.sub("", cleaned, count=1)
    cleaned = _strip_stage_direction_lines(cleaned)
    cleaned = _strip_inline_stage_direction_spans(cleaned)
    lines = [line.rstrip() for line in cleaned.splitlines()]
    cleaned = "\n".join(lines).strip()
    if removed_think:
        cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    cleaned = _strip_meta_reply_artifacts(cleaned)
    return cleaned


def _coerce_positive_int(value, default):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _split_nonempty_lines(raw_reply):
    return [line.strip() for line in str(raw_reply or "").splitlines() if line.strip()]


def _should_keep_unsplit(text):
    return bool(STRUCTURED_TEXT_RE.search(str(text or "")))


def _split_by_regex_keep_punct(text, pattern):
    return [
        match.group(0).strip()
        for match in pattern.finditer(str(text or ""))
        if match.group(0).strip()
    ]


def _refine_segments_by_length(segments, *, soft_max_chars, pattern):
    refined = []
    for segment in segments:
        if len(segment) <= soft_max_chars:
            refined.append(segment)
            continue
        children = _split_by_regex_keep_punct(segment, pattern)
        if len(children) > 1:
            refined.extend(children)
        else:
            refined.append(segment)
    return refined


def _merge_segments_by_limits(segments, *, max_count, soft_max_chars):
    if not segments:
        return []

    parts = [segment for segment in segments if str(segment or "").strip()]
    if len(parts) <= max_count:
        return parts

    lengths = [_split_reply_text_length(part) for part in parts]
    prefix_lengths = [0]
    for length in lengths:
        prefix_lengths.append(prefix_lengths[-1] + length)

    total_length = prefix_lengths[-1]
    target_lengths = _build_rebalanced_target_lengths(
        total_length,
        group_count=max_count,
        soft_max_chars=soft_max_chars,
    )
    dp = [[None] * (max_count + 1) for _ in range(len(parts) + 1)]
    dp[0][0] = (0.0, None)

    for used in range(1, max_count + 1):
        min_end = used
        max_end = len(parts) - (max_count - used)
        for end in range(min_end, max_end + 1):
            best = None
            for start in range(used - 1, end):
                previous = dp[start][used - 1]
                if previous is None:
                    continue
                part_length = prefix_lengths[end] - prefix_lengths[start]
                score = previous[0] + _rebalanced_group_penalty(
                    part_length,
                    group_index=used - 1,
                    group_count=max_count,
                    target_lengths=target_lengths,
                    soft_max_chars=soft_max_chars,
                )
                if best is None or score < best[0]:
                    best = (score, start)
            dp[end][used] = best

    if dp[len(parts)][max_count] is None:
        while len(parts) > max_count:
            tail = parts.pop()
            parts[-1] = f"{parts[-1]}{tail}"
        return parts

    rebuilt = []
    cursor = len(parts)
    used = max_count
    while used > 0:
        entry = dp[cursor][used]
        if entry is None or entry[1] is None:
            rebuilt = []
            break
        start = entry[1]
        rebuilt.append("".join(parts[start:cursor]))
        cursor = start
        used -= 1

    if not rebuilt:
        while len(parts) > max_count:
            tail = parts.pop()
            parts[-1] = f"{parts[-1]}{tail}"
        return parts
    rebuilt.reverse()
    return rebuilt


def _split_reply_text_length(part_text):
    return len(re.sub(r"\s+", "", str(part_text or "")))


def _build_rebalanced_target_lengths(total_length, *, group_count, soft_max_chars):
    if group_count <= 1:
        return [float(total_length)]
    if total_length > soft_max_chars * group_count:
        targets = [float(soft_max_chars)] * group_count
        targets[0] = max(1.0, float(soft_max_chars) * 0.85)
        targets[-1] = max(float(soft_max_chars), float(total_length) - sum(targets[:-1]))
        return targets

    weights = [0.85] + [1.0] * (group_count - 1)
    unit = float(total_length) / sum(weights)
    return [weight * unit for weight in weights]


def _rebalanced_group_penalty(part_length, *, group_index, group_count, target_lengths, soft_max_chars):
    target = float(target_lengths[group_index])
    penalty = (float(part_length) - target) ** 2
    is_last = group_index == group_count - 1
    if not is_last:
        overflow = max(0.0, float(part_length) - float(soft_max_chars))
        penalty += (overflow ** 2) * 6.0
        min_ratio = 0.4 if group_count >= 3 and group_index == 0 else 0.5
        min_preferred = max(1.0, float(soft_max_chars) * min_ratio)
        shortage = max(0.0, min_preferred - float(part_length))
        penalty += (shortage ** 2) * 4.0
        if group_count >= 3 and group_index == 0:
            first_soft_cap = float(soft_max_chars) * 0.95
            first_overflow = max(0.0, float(part_length) - first_soft_cap)
            penalty += (first_overflow ** 2) * 0.6
    else:
        tail_soft_cap = max(float(soft_max_chars), target)
        overflow = max(0.0, float(part_length) - tail_soft_cap)
        penalty += (overflow ** 2) * 0.35
    return penalty


def _strip_plain_terminal_punctuation(text):
    return str(text or "").rstrip().rstrip(PLAIN_TERMINAL_PUNCTUATION)


def _split_chinese_space_pauses(text, *, max_count):
    stripped = str(text or "").strip()
    if not stripped or "\n" in stripped:
        return []
    if " " not in stripped:
        return []
    if not CHINESE_SPACE_REPLY_RE.fullmatch(stripped):
        return []

    parts = [part.strip() for part in re.split(r"\s+", stripped) if part.strip()]
    if len(parts) < 2 or len(parts) > 3 or len(parts) > max_count:
        return []
    for part in parts:
        if not re.search(r"[\u4e00-\u9fff]", part):
            return []
        if _split_reply_text_length(part) > 20:
            return []
    return [_strip_plain_terminal_punctuation(part) for part in parts]


def local_split_reply(reply, *, max_count, soft_max_chars):
    text = str(reply or "")
    max_count = _coerce_positive_int(max_count, AUTO_SPLIT_MIN_LINES)
    soft_max_chars = _coerce_positive_int(soft_max_chars, AUTO_SPLIT_MAX_LINE_CHARS)
    if not text.strip():
        return [reply]
    if max_count <= 1 or _should_keep_unsplit(text):
        return [reply]

    working = HTML_BREAK_RE.sub("\n", text)
    if "\n" in working:
        parts = [_strip_plain_terminal_punctuation(part) for part in _split_nonempty_lines(working)]
        parts = [part for part in parts if part.strip()]
        return parts or [reply]

    if len(working.strip()) <= soft_max_chars:
        return [reply]

    segments = _split_nonempty_lines(working)
    if len(segments) <= 1:
        whole = working.strip()
        if not whole:
            segments = []
        else:
            sentence_segments = _split_by_regex_keep_punct(whole, SENTENCE_BOUNDARY_RE)
            segments = sentence_segments if len(sentence_segments) > 1 else [whole]
    segments = _refine_segments_by_length(
        segments,
        soft_max_chars=soft_max_chars,
        pattern=SENTENCE_BOUNDARY_RE,
    )
    segments = _refine_segments_by_length(
        segments,
        soft_max_chars=soft_max_chars,
        pattern=COMMA_BOUNDARY_RE,
    )
    parts = _merge_segments_by_limits(
        segments,
        max_count=max_count,
        soft_max_chars=soft_max_chars,
    )
    parts = [_strip_plain_terminal_punctuation(part) for part in parts]
    parts = [part for part in parts if part.strip()]
    return parts or [reply]


def parse_split_reply_with_source(
    reply,
    max_count,
    *,
    max_chars=AUTO_SPLIT_MAX_LINE_CHARS,
    allow_chinese_space_split=False,
):
    """Parse an AI reply into sendable parts and report which strategy matched."""
    raw_reply = str(reply or "")
    max_count = _coerce_positive_int(max_count, AUTO_SPLIT_MIN_LINES)
    max_chars = _coerce_positive_int(max_chars, AUTO_SPLIT_MAX_LINE_CHARS)
    if not raw_reply.strip():
        return [reply], SPLIT_SOURCE_NONE

    if allow_chinese_space_split:
        space_parts = _split_chinese_space_pauses(raw_reply, max_count=max_count)
        if len(space_parts) > 1:
            return space_parts, SPLIT_SOURCE_SPACE

    parts = local_split_reply(raw_reply, max_count=max_count, soft_max_chars=max_chars)
    if len(parts) <= 1:
        return parts, SPLIT_SOURCE_NONE
    if "\n" in raw_reply:
        return parts, SPLIT_SOURCE_NEWLINE
    if any(punct in raw_reply for punct in "。！？!?～~"):
        return parts, SPLIT_SOURCE_SENTENCE
    return parts, SPLIT_SOURCE_SENTENCE


def parse_split_reply(
    reply,
    max_count,
    *,
    max_chars=AUTO_SPLIT_MAX_LINE_CHARS,
    allow_chinese_space_split=False,
):
    """Parse an AI reply into sendable parts."""
    parts, _source = parse_split_reply_with_source(
        reply,
        max_count,
        max_chars=max_chars,
        allow_chinese_space_split=allow_chinese_space_split,
    )
    return parts


def clean_reply_for_send(
    reply,
    *,
    clean_enabled,
    fallback_reply,
    blocked_policy=DEFAULT_BLOCKED_POLICY,
    on_clean_empty=None,
):
    if not clean_enabled:
        return reply
    cleaned = clean_ai_reply_text(reply)
    if cleaned:
        return cleaned
    if on_clean_empty:
        on_clean_empty()
    if blocked_policy == "silent":
        return ""
    return fallback_reply


def prepare_reply_parts(
    reply,
    *,
    split_enabled,
    max_count,
    clean_enabled,
    fallback_reply,
    blocked_policy=DEFAULT_BLOCKED_POLICY,
    force_single=False,
    max_chars=AUTO_SPLIT_MAX_LINE_CHARS,
    allow_chinese_space_split=False,
    on_clean_empty=None,
):
    parts, _source, _source_count = prepare_reply_parts_with_source(
        reply,
        split_enabled=split_enabled,
        max_count=max_count,
        clean_enabled=clean_enabled,
        fallback_reply=fallback_reply,
        blocked_policy=blocked_policy,
        force_single=force_single,
        max_chars=max_chars,
        allow_chinese_space_split=allow_chinese_space_split,
        on_clean_empty=on_clean_empty,
    )
    return parts


def prepare_reply_parts_with_source(
    reply,
    *,
    split_enabled,
    max_count,
    clean_enabled,
    fallback_reply,
    blocked_policy=DEFAULT_BLOCKED_POLICY,
    force_single=False,
    max_chars=AUTO_SPLIT_MAX_LINE_CHARS,
    allow_chinese_space_split=False,
    on_clean_empty=None,
):
    if force_single:
        return [reply], SPLIT_SOURCE_NONE, 1
    reply_for_split = reply
    if split_enabled and clean_enabled:
        reply_for_split = clean_ai_reply_text(reply)
        if not reply_for_split:
            if on_clean_empty:
                on_clean_empty()
            if blocked_policy == "silent":
                return [], SPLIT_SOURCE_NONE, 0
            return [fallback_reply], SPLIT_SOURCE_NONE, 1
    source_parts, split_source = (
        parse_split_reply_with_source(
            reply_for_split,
            max_count,
            max_chars=max_chars,
            allow_chinese_space_split=allow_chinese_space_split,
        )
        if split_enabled
        else ([reply_for_split], SPLIT_SOURCE_NONE)
    )
    prepared_parts = [
        clean_reply_for_send(
            part,
            clean_enabled=clean_enabled,
            fallback_reply=fallback_reply,
            blocked_policy=blocked_policy,
            on_clean_empty=on_clean_empty,
        )
        for part in source_parts
    ]
    return [part for part in prepared_parts if part], split_source, len(source_parts)
