"""Keyword reply business rules."""

import re

from core.message_pipeline import contains_group_mention

MAX_KEYWORD_REPLY_FILES = 9
KEYWORD_TERM_SPLIT_RE = re.compile(r"[\r\n,，;；]+")


def normalize_keyword_terms(value):
    """Return a clean keyword term list from a string or list."""
    raw_terms = []
    if isinstance(value, str):
        raw_terms = KEYWORD_TERM_SPLIT_RE.split(value)
    elif isinstance(value, list):
        raw_terms = value

    terms = []
    seen = set()
    for item in raw_terms:
        term = str(item or "").strip()
        if not term or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def join_keyword_terms(terms):
    """Return the canonical display form for keyword input."""
    return "；".join(normalize_keyword_terms(terms))


def normalize_keyword_reply_rule(keyword, reply):
    """Return a clean keyword reply rule with keywords, text, and files."""
    keywords = []
    text = ""
    files = []

    if isinstance(reply, str):
        text = reply.strip()
    elif isinstance(reply, dict):
        keywords = normalize_keyword_terms(reply.get("keywords"))
        text = str(reply.get("text") or "").strip()
        raw_files = reply.get("files")
        if isinstance(raw_files, list):
            for path in raw_files:
                normalized_path = str(path or "").strip()
                if not normalized_path:
                    continue
                files.append(normalized_path)
                if len(files) >= MAX_KEYWORD_REPLY_FILES:
                    break

    if not keywords:
        keywords = normalize_keyword_terms(keyword)
    if not keywords:
        return None
    return {"keywords": keywords, "text": text, "files": files}


def normalize_keyword_reply_actions(reply):
    """Return a clean ordered action list for fixed text + files replies."""
    if not isinstance(reply, dict):
        return []

    actions = []
    content = str(reply.get("text") or "").strip()
    if content:
        actions.append({"type": "text", "content": content})

    files = reply.get("files")
    if isinstance(files, list):
        for path in files:
            normalized_path = str(path or "").strip()
            if not normalized_path:
                continue
            actions.append({"type": "file", "path": normalized_path})
            if len(actions) - (1 if content else 0) >= MAX_KEYWORD_REPLY_FILES:
                break
    return actions


def find_keyword_reply(keyword_dict, content):
    """Return the first keyword reply matching content, preserving config order."""
    content = str(content or "")
    for keyword, reply in (keyword_dict or {}).items():
        normalized_rule = normalize_keyword_reply_rule(keyword, reply)
        if not normalized_rule:
            continue
        for term in normalized_rule["keywords"]:
            if term in content:
                return {"keyword": str(keyword), "reply": normalized_rule}
    return None


def plan_private_keyword_reply(enabled, keyword_dict, content):
    """Return a private keyword reply plan when keyword replies are enabled."""
    if not enabled:
        return None
    return find_keyword_reply(keyword_dict, content)


def plan_group_keyword_reply(enabled, keyword_dict, content, *, at_only=False, at_marker=""):
    """Return a group keyword reply plan after applying the optional @ gate."""
    if not enabled:
        return None
    content = str(content or "")
    if at_only and not contains_group_mention(content, at_marker):
        return None
    return find_keyword_reply(keyword_dict, content)
