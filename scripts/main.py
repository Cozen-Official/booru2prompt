import base64
import json
import os
import re
import shutil
from functools import lru_cache
from urllib.request import urlopen, Request
from urllib import parse
from urllib.error import HTTPError, URLError
import inspect

import gradio as gr

import modules.ui
from modules import script_callbacks, scripts

#The auto1111 guide on developing extensions says to use scripts.basedir() to get the current directory
#However, for some reason, this kept returning the stable diffusion root instead.
#So this is my janky workaround to get this extensions directory.
edirectory = inspect.getfile(lambda: None)
edirectory = edirectory[:edirectory.find("scripts")]

DEFAULT_HEADERS = {
    "User-Agent": "booru2prompt/1.1 (+https://github.com/Malisius/booru2prompt)",
}

SYSTEM_DISPLAY_NAMES = {
    "auto": "Auto (detect)",
    "danbooru": "Danbooru",
    "e621": "e621",
    "moebooru": "Moebooru",
    "gelbooru": "Gelbooru",
    "philomena": "Philomena",
}

SUPPORTED_SYSTEMS = tuple(SYSTEM_DISPLAY_NAMES.keys())
SYSTEM_NAME_LOOKUP = {v: k for k, v in SYSTEM_DISPLAY_NAMES.items()}

def loadsettings():
    """Return a dictionary of settings read from settings.json in the extension directory

    Returns:
        dict: settings and api keys
    """    
    print("Loading booru2prompt settings")
    with open(edirectory + "settings.json", encoding="utf-8") as file:
        settings = json.load(file)

    if "boorus" not in settings:
        settings["boorus"] = []

    for booru in settings["boorus"]:
        booru.setdefault("username", "")
        booru.setdefault("apikey", "")
        booru.setdefault("cookie", "")
        booru.setdefault("system", "auto")
        if booru["system"] not in SUPPORTED_SYSTEMS:
            booru["system"] = "auto"

    if settings.get("boorus") and settings.get("active") not in [b["name"] for b in settings["boorus"]]:
        settings["active"] = settings["boorus"][0]["name"]

    return settings

def _booru_names():
    return [booru["name"] for booru in settings["boorus"]]

def _find_booru_index(name):
    for index, booru in enumerate(settings["boorus"]):
        if booru["name"] == name:
            return index
    return None

def _ensure_active(preferred=None):
    booru_names = _booru_names()
    if not booru_names:
        settings["active"] = ""
        return ""

    if preferred and preferred in booru_names:
        settings["active"] = preferred
    elif settings.get("active") not in booru_names:
        settings["active"] = booru_names[0]

    return settings["active"]

def _normalize_host(host):
    host = (host or "").strip()
    if not host:
        raise gr.Error("Host URL cannot be empty.")

    parsed = parse.urlparse(host)
    if not parsed.scheme:
        host = "https://" + host
        parsed = parse.urlparse(host)

    if parsed.scheme not in ("http", "https"):
        raise gr.Error("Host URL must start with http:// or https://.")
    if not parsed.netloc:
        raise gr.Error("Host URL must include a domain.")

    normalized_path = parsed.path.rstrip("/")
    normalized_host = f"{parsed.scheme}://{parsed.netloc}"
    if normalized_path:
        normalized_host += normalized_path

    return normalized_host

def _persist_settings():
    with open(edirectory + "settings.json", "w", encoding="utf-8") as file:
        json.dump(settings, file, indent=4)

def _build_settings_outputs():
    active_name = _ensure_active()
    booru_names = _booru_names()

    if not booru_names:
        return (
            gr.Dropdown.update(choices=[], value=None),
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        )

    booru = next((b for b in settings["boorus"] if b["name"] == active_name), None)
    if booru is None:
        raise gr.Error(f"Booru '{active_name}' was not found.")

    system_value = booru.get("system", "auto")
    system_display = SYSTEM_DISPLAY_NAMES.get(system_value, SYSTEM_DISPLAY_NAMES["auto"])

    return (
        gr.Dropdown.update(choices=booru_names, value=active_name),
        booru.get("name", ""),
        booru.get("host", ""),
        booru.get("username", ""),
        booru.get("apikey", ""),
        booru.get("cookie", ""),
        system_display,
        active_name,
        active_name,
    )

def _sanitize_url_for_logging(url):
    parsed = parse.urlparse(url)
    query_items = parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = []
    for key, value in query_items:
        if key.lower() in {"login", "api_key", "password_hash", "user_id", "key"} and value:
            redacted.append((key, "***"))
        else:
            redacted.append((key, value))

    sanitized = parsed._replace(query=parse.urlencode(redacted))
    print(parse.urlunparse(sanitized))

def _append_query(url, params):
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{parse.urlencode(params)}"

def _build_auth_headers(username="", apikey="", *, auth_mode="danbooru"):
    username = (username or "").strip()
    apikey = (apikey or "").strip()

    if not username or not apikey:
        return {}

    if auth_mode in ("danbooru", "e621"):
        token = f"{username}:{apikey}".encode("utf-8")
        encoded = base64.b64encode(token).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}

    return {}


def _build_request_headers(username="", apikey="", cookie="", *, auth_mode="danbooru"):
    headers = _build_auth_headers(username, apikey, auth_mode=auth_mode)
    if headers:
        headers = dict(headers)
    else:
        headers = {}

    cookie = (cookie or "").strip()
    if cookie:
        headers["Cookie"] = cookie

    return headers


def _fetch_json(url, *, headers=None, raise_for_status=True):
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)
    request = Request(url, data=None, headers=merged_headers)
    try:
        with urlopen(request) as response:
            payload = response.read()
    except (HTTPError, URLError) as error:
        if raise_for_status:
            raise
        return None

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        if raise_for_status:
            raise
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if "challenge-container" in text and "X-Verification-Challenge" in text:
            raise gr.Error(
                "The booru responded with an interactive verification challenge. "
                "Open the booru in a browser, complete the verification, and paste "
                "the resulting session cookie into the booru's settings before "
                "retrying."
            )
        if raise_for_status:
            raise
        return None


def _safe_fetch_json(url, *, description, headers=None):
    try:
        return _fetch_json(url, headers=headers)
    except HTTPError as error:
        raise gr.Error(f"Failed to {description}: HTTP {error.code}. The booru may require authentication or the endpoint may not exist.") from error
    except URLError as error:
        raise gr.Error(f"Failed to {description}: {error.reason}.") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise gr.Error(
            f"Failed to {description}: The booru returned a non-JSON response. "
            "This may indicate an authentication error, an incorrect booru system type, or that the API is unavailable. "
            "Try verifying your credentials or manually selecting the correct booru system type in Settings."
        ) from error

def _query_with_auth(params, username="", apikey="", *, auth_mode="danbooru"):
    params = dict(params)
    if auth_mode in ("danbooru", "e621"):
        if apikey:
            params["api_key"] = apikey
        if username:
            params["login"] = username
    elif auth_mode == "moebooru":
        if username:
            params["login"] = username
        if apikey:
            params["password_hash"] = apikey
    elif auth_mode == "gelbooru":
        if username:
            params["user_id"] = username
        if apikey:
            params["api_key"] = apikey
    elif auth_mode == "philomena":
        if apikey:
            params["key"] = apikey
    return params

def _absolute_url(host, value):
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return host.rstrip("/") + ("/" if not value.startswith("/") else "") + value

def _normalize_tags(value):
    if not value:
        return []
    if isinstance(value, str):
        return [tag for tag in value.split() if tag]

    normalized = []
    for item in value:
        if not item:
            continue
        if isinstance(item, str):
            normalized.append(item.replace(" ", "_").strip())
        else:
            normalized.append(str(item))
    return [tag for tag in normalized if tag]

def _prepare_local_image_path(index, source_url):
    parsed = parse.urlparse(source_url)
    _, ext = os.path.splitext(parsed.path)
    if not ext or len(ext) > 6:
        ext = ".jpg"
    filename = f"temp{index}{ext}"
    return os.path.join(edirectory, "tempimages", filename)


def _download_to_path(url, destination, *, headers=None):
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)

    request = Request(url, data=None, headers=merged_headers)
    with urlopen(request) as response, open(destination, "wb") as file:
        shutil.copyfileobj(response, file)


def _build_tag_query(query, removeanimated):
    query = (query or "").strip()
    if removeanimated:
        if query:
            query += " "
        query += "-animated"
    return query.strip()

def _extract_post_id(reference, host):
    if not isinstance(reference, str):
        return None, None

    trimmed = reference.strip()
    if not trimmed:
        return None, None

    if trimmed.startswith("id:"):
        return trimmed[3:], None

    if not trimmed.startswith("http"):
        return trimmed, None

    parsed = parse.urlparse(trimmed)
    host_netloc = parse.urlparse(host).netloc
    if parsed.netloc and host_netloc and parsed.netloc != host_netloc:
        raise gr.Error("The provided URL does not match the selected booru.")

    query = parse.parse_qs(parsed.query)
    if "id" in query and query["id"]:
        return query["id"][0], trimmed

    path_parts = [part for part in parsed.path.split("/") if part]
    for part in reversed(path_parts):
        if re.fullmatch(r"\d+", part):
            return part, trimmed

    return None, trimmed

def _normalize_post_general(post, *, image_url, artist=None, character=None, copyright=None, meta=None):
    return {
        "general": _normalize_tags(post),
        "artist": _normalize_tags(artist or []),
        "character": _normalize_tags(character or []),
        "copyright": _normalize_tags(copyright or []),
        "meta": _normalize_tags(meta or []),
        "image_url": image_url,
    }

def _normalize_danbooru_post(post):
    image_url = post.get("large_file_url") or post.get("file_url") or post.get("preview_file_url")
    return _normalize_post_general(
        post.get("tag_string_general"),
        image_url=image_url,
        artist=post.get("tag_string_artist"),
        character=post.get("tag_string_character"),
        copyright=post.get("tag_string_copyright"),
        meta=post.get("tag_string_meta"),
    )

def _normalize_e621_post(post):
    tags = post.get("tags", {})
    general = []
    for key in ("general", "species", "lore"):
        general.extend(tags.get(key, []))
    image_data = post.get("file", {})
    image_url = image_data.get("url") or post.get("sample", {}).get("url") or post.get("preview", {}).get("url")
    return _normalize_post_general(
        general,
        image_url=image_url,
        artist=tags.get("artist", []),
        character=tags.get("character", []),
        copyright=tags.get("copyright", []),
        meta=tags.get("meta", []),
    )

def _normalize_moebooru_post(post):
    image_url = post.get("file_url") or post.get("jpeg_url") or post.get("sample_url") or post.get("preview_url")
    return _normalize_post_general(post.get("tags", ""), image_url=image_url)

def _normalize_gelbooru_post(post):
    image_url = post.get("file_url") or post.get("sample_url") or post.get("preview_url")
    return _normalize_post_general(post.get("tags", ""), image_url=image_url)

def _normalize_philomena_post(post):
    tags = post.get("tags", [])
    general = []
    artist = []
    character = []
    for tag in tags:
        lower = tag.lower()
        if lower.startswith("artist:"):
            artist.append(tag.split(":", 1)[1])
        elif lower.startswith("character:") or lower.startswith("oc:"):
            character.append(tag.split(":", 1)[1])
        else:
            general.append(tag)

    image_url = post.get("representations", {}).get("full") or post.get("view_url")
    return _normalize_post_general(general, image_url=image_url, artist=artist, character=character)

@lru_cache(maxsize=None)
def detect_booru_type(host, username="", apikey="", cookie=""):
    host = (host or "").rstrip("/")
    username = username or ""
    apikey = apikey or ""
    cookie = cookie or ""

    detectors = [
        ("Danbooru/e621", lambda: _detect_danbooru(host, username, apikey, cookie)),
        ("Moebooru", lambda: _detect_moebooru(host, username, apikey, cookie)),
        ("Gelbooru", lambda: _detect_gelbooru(host, username, apikey, cookie)),
        ("Philomena", lambda: _detect_philomena(host, username, apikey, cookie)),
    ]

    for name, detector in detectors:
        booru_type = detector()
        if booru_type:
            print(f"Detected booru type: {booru_type} (matched {name} pattern)")
            return booru_type

    raise gr.Error(
        "Unable to determine the booru type. The API did not match any known booru systems. "
        "Please verify the host URL and credentials, or manually select the booru system type in Settings."
    )

def _detect_danbooru(host, username, apikey, cookie):
    params = _query_with_auth({"limit": 1}, username, apikey, auth_mode="danbooru")
    url = f"{host}/posts.json?{parse.urlencode(params)}"
    headers = _build_request_headers(username, apikey, cookie, auth_mode="danbooru")
    data = _fetch_json(url, headers=headers, raise_for_status=False)
    if not data:
        return None

    if isinstance(data, dict) and "posts" in data:
        return "e621"

    if isinstance(data, list) and data and isinstance(data[0], dict) and "tag_string_general" in data[0]:
        return "danbooru"

    return None

def _detect_moebooru(host, username, apikey, cookie):
    params = _query_with_auth({"limit": 1}, username, apikey, auth_mode="moebooru")
    url = f"{host}/post.json?{parse.urlencode(params)}"
    headers = _build_request_headers(username, apikey, cookie, auth_mode="moebooru")
    data = _fetch_json(url, headers=headers, raise_for_status=False)
    if isinstance(data, list) and data and isinstance(data[0], dict) and "tags" in data[0]:
        return "moebooru"
    return None

def _detect_gelbooru(host, username, apikey, cookie):
    params = _query_with_auth(
        {
            "page": "dapi",
            "s": "post",
            "q": "index",
            "json": 1,
            "limit": 1,
        },
        username,
        apikey,
        auth_mode="gelbooru",
    )
    url = f"{host}/index.php?{parse.urlencode(params)}"
    headers = _build_request_headers(username, apikey, cookie, auth_mode="gelbooru")
    data = _fetch_json(url, headers=headers, raise_for_status=False)
    if isinstance(data, dict) and "post" in data:
        posts = data["post"]
        if isinstance(posts, dict) or (isinstance(posts, list) and posts):
            return "gelbooru"
    if isinstance(data, list) and data:
        return "gelbooru"
    return None

def _detect_philomena(host, username, apikey, cookie):
    params = _query_with_auth({"q": "id.gt:0", "per_page": 1, "page": 1}, username, apikey, auth_mode="philomena")
    url = f"{host}/api/v1/json/search/images?{parse.urlencode(params)}"
    headers = _build_request_headers(username, apikey, cookie, auth_mode="philomena")
    data = _fetch_json(url, headers=headers, raise_for_status=False)
    if isinstance(data, dict) and data.get("images") is not None:
        return "philomena"
    return None

def _search_danbooru(host, username, apikey, cookie, tags, page, limit):
    params = _query_with_auth({"limit": limit, "page": page}, username, apikey, auth_mode="danbooru")
    params["tags"] = tags
    url = f"{host}/posts.json?{parse.urlencode(params)}"
    _sanitize_url_for_logging(url)
    headers = _build_request_headers(username, apikey, cookie, auth_mode="danbooru")
    data = _safe_fetch_json(url, description="search the booru", headers=headers)
    posts = data.get("posts", []) if isinstance(data, dict) else data
    if posts is None:
        posts = []
    if not isinstance(posts, list):
        raise gr.Error("Booru returned an unexpected search payload.")

    results = []
    for post in posts:
        if not isinstance(post, dict) or post.get("id") is None:
            continue
        normalized = _normalize_danbooru_post(post)
        image_url = normalized.get("image_url")
        if not image_url:
            continue
        results.append({"id": str(post["id"]), "image_url": image_url})
    return results

def _search_e621(host, username, apikey, cookie, tags, page, limit):
    params = _query_with_auth({"limit": limit, "page": page}, username, apikey, auth_mode="e621")
    params["tags"] = tags
    url = f"{host}/posts.json?{parse.urlencode(params)}"
    _sanitize_url_for_logging(url)
    headers = _build_request_headers(username, apikey, cookie, auth_mode="e621")
    data = _safe_fetch_json(url, description="search the booru", headers=headers)
    posts = data.get("posts", []) if isinstance(data, dict) else []
    if not isinstance(posts, list):
        raise gr.Error("Booru returned an unexpected search payload.")

    results = []
    for post in posts:
        if not isinstance(post, dict) or post.get("id") is None:
            continue
        normalized = _normalize_e621_post(post)
        image_url = normalized.get("image_url")
        if not image_url:
            continue
        results.append({"id": str(post["id"]), "image_url": image_url})
    return results

def _search_moebooru(host, username, apikey, cookie, tags, page, limit):
    params = _query_with_auth({"limit": limit, "page": page, "tags": tags}, username, apikey, auth_mode="moebooru")
    url = f"{host}/post.json?{parse.urlencode(params)}"
    _sanitize_url_for_logging(url)
    headers = _build_request_headers(username, apikey, cookie, auth_mode="moebooru")
    data = _safe_fetch_json(url, description="search the booru", headers=headers)
    if data is None:
        return []
    if not isinstance(data, list):
        raise gr.Error("Booru returned an unexpected search payload.")

    results = []
    for post in data:
        if not isinstance(post, dict) or post.get("id") is None:
            continue
        normalized = _normalize_moebooru_post(post)
        image_url = normalized.get("image_url")
        if not image_url:
            continue
        results.append({"id": str(post["id"]), "image_url": image_url})
    return results

def _search_gelbooru(host, username, apikey, cookie, tags, page, limit):
    pid = max(page - 1, 0)
    params = _query_with_auth(
        {
            "page": "dapi",
            "s": "post",
            "q": "index",
            "json": 1,
            "limit": limit,
            "tags": tags,
            "pid": pid,
        },
        username,
        apikey,
        auth_mode="gelbooru",
    )
    url = f"{host}/index.php?{parse.urlencode(params)}"
    _sanitize_url_for_logging(url)
    headers = _build_request_headers(username, apikey, cookie, auth_mode="gelbooru")
    data = _safe_fetch_json(url, description="search the booru", headers=headers)
    if isinstance(data, dict):
        posts = data.get("post", [])
        if isinstance(posts, dict):
            posts = [posts]
    else:
        posts = data
    if posts is None:
        posts = []
    if not isinstance(posts, list):
        raise gr.Error("Booru returned an unexpected search payload.")

    results = []
    for post in posts:
        if not isinstance(post, dict) or post.get("id") is None:
            continue
        normalized = _normalize_gelbooru_post(post)
        image_url = normalized.get("image_url")
        if not image_url:
            continue
        results.append({"id": str(post["id"]), "image_url": image_url})
    return results

def _search_philomena(host, username, apikey, cookie, tags, page, limit):
    tokens = [token for token in (tags or "").split() if token]
    query_value = ",".join(tokens) if tokens else "*"
    params = _query_with_auth({"q": query_value, "per_page": limit, "page": page}, username, apikey, auth_mode="philomena")
    url = f"{host}/api/v1/json/search/images?{parse.urlencode(params)}"
    _sanitize_url_for_logging(url)
    headers = _build_request_headers(username, apikey, cookie, auth_mode="philomena")
    data = _safe_fetch_json(url, description="search the booru", headers=headers)
    images = data.get("images", []) if isinstance(data, dict) else []
    if not isinstance(images, list):
        raise gr.Error("Booru returned an unexpected search payload.")

    results = []
    for image in images:
        if not isinstance(image, dict) or image.get("id") is None:
            continue
        normalized = _normalize_philomena_post(image)
        image_url = normalized.get("image_url")
        if not image_url:
            continue
        results.append({"id": str(image["id"]), "image_url": image_url})
    return results

SEARCH_HANDLERS = {
    "danbooru": _search_danbooru,
    "e621": _search_e621,
    "moebooru": _search_moebooru,
    "gelbooru": _search_gelbooru,
    "philomena": _search_philomena,
}

def _fetch_danbooru_post(host, username, apikey, cookie, post_id, reference_url):
    params = _query_with_auth({}, username, apikey, auth_mode="danbooru")
    if post_id:
        url = _append_query(f"{host}/posts/{post_id}.json", params)
    elif reference_url:
        cleaned = reference_url.split("?")[0]
        if not cleaned.endswith(".json"):
            cleaned += ".json"
        url = _append_query(cleaned, params)
    else:
        raise gr.Error("Unable to determine which post to load.")

    _sanitize_url_for_logging(url)
    headers = _build_request_headers(username, apikey, cookie, auth_mode="danbooru")
    data = _safe_fetch_json(url, description="load post details", headers=headers)
    if isinstance(data, dict):
        return _normalize_danbooru_post(data)
    raise gr.Error("Booru returned an unexpected payload when loading the post.")

def _fetch_e621_post(host, username, apikey, cookie, post_id, reference_url):
    if not post_id:
        raise gr.Error("Unable to determine which post to load.")
    params = _query_with_auth({}, username, apikey, auth_mode="e621")
    url = _append_query(f"{host}/posts/{post_id}.json", params)
    _sanitize_url_for_logging(url)
    headers = _build_request_headers(username, apikey, cookie, auth_mode="e621")
    data = _safe_fetch_json(url, description="load post details", headers=headers)
    if isinstance(data, dict) and isinstance(data.get("post"), dict):
        return _normalize_e621_post(data["post"])
    raise gr.Error("Booru returned an unexpected payload when loading the post.")

def _fetch_moebooru_post(host, username, apikey, cookie, post_id, reference_url):
    if not post_id:
        raise gr.Error("Unable to determine which post to load.")
    params = _query_with_auth({"tags": f"id:{post_id}", "limit": 1}, username, apikey, auth_mode="moebooru")
    url = f"{host}/post.json?{parse.urlencode(params)}"
    _sanitize_url_for_logging(url)
    headers = _build_request_headers(username, apikey, cookie, auth_mode="moebooru")
    data = _safe_fetch_json(url, description="load post details", headers=headers)
    if isinstance(data, list) and data:
        return _normalize_moebooru_post(data[0])
    raise gr.Error("Post could not be found on the selected booru.")

def _fetch_gelbooru_post(host, username, apikey, cookie, post_id, reference_url):
    if not post_id:
        raise gr.Error("Unable to determine which post to load.")
    params = _query_with_auth(
        {
            "page": "dapi",
            "s": "post",
            "q": "index",
            "json": 1,
            "id": post_id,
            "limit": 1,
        },
        username,
        apikey,
        auth_mode="gelbooru",
    )
    url = f"{host}/index.php?{parse.urlencode(params)}"
    _sanitize_url_for_logging(url)
    headers = _build_request_headers(username, apikey, cookie, auth_mode="gelbooru")
    data = _safe_fetch_json(url, description="load post details", headers=headers)
    if isinstance(data, dict) and data.get("post"):
        posts = data["post"]
        if isinstance(posts, dict):
            return _normalize_gelbooru_post(posts)
        if isinstance(posts, list) and posts:
            return _normalize_gelbooru_post(posts[0])
    if isinstance(data, list) and data:
        return _normalize_gelbooru_post(data[0])
    raise gr.Error("Post could not be found on the selected booru.")

def _fetch_philomena_post(host, username, apikey, cookie, post_id, reference_url):
    if not post_id:
        raise gr.Error("Unable to determine which post to load.")
    params = _query_with_auth({}, username, apikey, auth_mode="philomena")
    url = _append_query(f"{host}/api/v1/json/images/{post_id}", params)
    _sanitize_url_for_logging(url)
    headers = _build_request_headers(username, apikey, cookie, auth_mode="philomena")
    data = _safe_fetch_json(url, description="load post details", headers=headers)
    if isinstance(data, dict) and isinstance(data.get("image"), dict):
        return _normalize_philomena_post(data["image"])
    raise gr.Error("Post could not be found on the selected booru.")

POST_FETCHERS = {
    "danbooru": _fetch_danbooru_post,
    "e621": _fetch_e621_post,
    "moebooru": _fetch_moebooru_post,
    "gelbooru": _fetch_gelbooru_post,
    "philomena": _fetch_philomena_post,
}

def savesettings(active, name, host, username, apikey, cookie, system_display, negprompt):
    """Persist updates to the currently selected booru.

    Args:
        active (str): The string identifier of the currently selected booru
        name (str): The updated display name for the booru
        host (str): The base URL for the booru
        username (str): The username for that booru
        apikey (str): The user's api key
        cookie (str): Session cookie string to include in requests
        system_display (str): The booru system to use for requests
        negprompt (str): The negative prompt to be appended to each image selection
    """
    original_name = active
    name = (name or "").strip()
    if not name:
        raise gr.Error("Booru name cannot be empty.")

    host = _normalize_host(host)

    system_value = SYSTEM_NAME_LOOKUP.get(system_display, system_display)
    if system_value not in SUPPORTED_SYSTEMS:
        raise gr.Error("Unsupported booru system selected.")

    booru_index = _find_booru_index(original_name)
    if booru_index is None:
        raise gr.Error(f"Booru '{original_name}' was not found.")

    if name != original_name and name in _booru_names():
        raise gr.Error(f"A booru named '{name}' already exists.")

    booru = settings["boorus"][booru_index]
    booru["name"] = name
    booru["host"] = host
    booru["username"] = username or ""
    booru["apikey"] = apikey or ""
    booru["cookie"] = cookie or ""
    booru["system"] = system_value

    settings["active"] = name
    settings["negativeprompt"] = negprompt

    _persist_settings()

    return _build_settings_outputs()

def addbooru(name, host, username, apikey, cookie, system_display, negprompt):
    name = (name or "").strip()
    if not name:
        raise gr.Error("Booru name cannot be empty.")

    host = _normalize_host(host)

    system_value = SYSTEM_NAME_LOOKUP.get(system_display, system_display)
    if system_value not in SUPPORTED_SYSTEMS:
        raise gr.Error("Unsupported booru system selected.")

    if name in _booru_names():
        raise gr.Error(f"A booru named '{name}' already exists.")

    settings["boorus"].append({
        "name": name,
        "host": host,
        "username": username or "",
        "apikey": apikey or "",
        "cookie": cookie or "",
        "system": system_value,
    })

    settings["active"] = name
    settings["negativeprompt"] = negprompt

    _persist_settings()

    return _build_settings_outputs()

def removebooru(active, negprompt):
    if len(settings.get("boorus", [])) <= 1:
        raise gr.Error("At least one booru must remain.")

    booru_index = _find_booru_index(active)
    if booru_index is None:
        raise gr.Error(f"Booru '{active}' was not found.")

    removed = settings["boorus"].pop(booru_index)

    settings["negativeprompt"] = negprompt

    if settings["active"] == removed["name"]:
        _ensure_active()
    else:
        _ensure_active(settings["active"])

    _persist_settings()

    return _build_settings_outputs()

#We're loading the settings here since all the further functions depend on this existing already
settings = loadsettings()

def getauth():
    """Get the username and api key for the currently selected booru

    Returns:
        tuple: (username, apikey) for whichever booru is selected in the dropdown
    """
    booru = _get_active_booru()
    if booru:
        return booru.get('username', ''), booru.get('apikey', '')
    return "", ""


def getcookie():
    """Get the session cookie for the currently selected booru."""

    booru = _get_active_booru()
    if booru:
        return booru.get('cookie', '')
    return ""

def gethost():
    """Get the url for the currently selected booru.
    This url will get piped straight into every request, so https:// should be
    included in each in settings.json if you want to use ssl.
    Furthermore, you should include a trailing slash in these urls, since they're already
    added by every other function here that uses this function.

    Returns:
        str: The full url for the selected booru
    """    
    booru = _get_active_booru()
    if booru:
        return booru.get('host', '')
    return ""

def _get_active_booru():
    active_name = _ensure_active()
    for booru in settings.get('boorus', []):
        if booru.get('name') == active_name:
            return booru
    return None

def searchbooru(query, removeanimated, curpage, pagechange=0):
    """Search the currently selected booru, and return a list of images and the current page.

    Args:
        query (str): A list of tags to search for, delimited by spaces
        removeanimated (bool): True to append -animated to searches
        curpage (str or int): The current page to search
        pagechange (int, optional): How much to change the current page by before searching. Defaults to 0.

    Returns:
        tuple (list, str): The list in this tuple is a list of tuples, where [0] is
        a str filepath to a locally saved image, and [1] is a string representation
        of the id for that image on the searched booru.
        The string in this return is new current page number, which may or may not have been changed.
    """
    host = gethost()
    u, a = getauth()
    cookie = getcookie()
    booru = _get_active_booru() or {}
    system_override = (booru.get("system", "auto") or "auto").lower()
    if system_override not in SUPPORTED_SYSTEMS:
        system_override = "auto"
    if system_override == "auto":
        booru_type = detect_booru_type(host, u, a, cookie)
    else:
        booru_type = system_override

    #If the page isn't changing, then the user almost certainly is initiating a new
    #search, so we can set the page number back to 1.
    if pagechange == 0:
        curpage = 1
    else:
        try:
            curpage = int(curpage)
        except (TypeError, ValueError):
            curpage = 1
        curpage = curpage + pagechange
        if curpage < 1:
            curpage = 1

    #We're about to use this in a url, so make it a string real quick
    curpage = str(curpage)

    handler = SEARCH_HANDLERS.get(booru_type)
    if handler is None:
        raise gr.Error(f"Search is not supported for booru type '{booru_type}'.")

    tags = _build_tag_query(query, removeanimated)
    results = handler(host, u, a, cookie, tags, int(curpage), 6)

    temp_dir = os.path.join(edirectory, "tempimages")
    os.makedirs(temp_dir, exist_ok=True)

    localimages = []
    for index, item in enumerate(results):
        image_url = _absolute_url(host, item.get("image_url"))
        if not image_url:
            continue

        savepath = _prepare_local_image_path(index, image_url)
        request_headers = _build_request_headers(u, a, cookie, auth_mode=booru_type)
        try:
            _download_to_path(image_url, savepath, headers=request_headers)
        except Exception as error:
            print(f"Failed to cache preview {image_url}: {error}")
            continue

        localimages.append((savepath, f"id:{item['id']}"))

    return localimages, curpage

def gotonextpage(query, removeanimated, curpage):
    return searchbooru(query, removeanimated, curpage, pagechange=1)

def gotoprevpage(query, removeanimated, curpage):
    return searchbooru(query, removeanimated, curpage, pagechange=-1)

def updatesettings(active = settings['active']):
    """Update the relevant textboxes in Gradio with the appropriate data when
    the user selects a new booru in the dropdown

    Args:
        active (str, optional): The str name of the booru the user switched to. Defaults to settings['active'].

    Returns:
        (str, str, str, str, str, str): The username, apikey, current booru label text,
        duplicate label text, editable booru name, and host URL for the selected booru.
    """
    active_name = _ensure_active(active)

    booru = next((b for b in settings['boorus'] if b['name'] == active_name), None)

    if not booru:
        system_display = SYSTEM_DISPLAY_NAMES["auto"]
        return "", "", "", active_name, active_name, "", "", system_display

    system_display = SYSTEM_DISPLAY_NAMES.get(booru.get('system', 'auto'), SYSTEM_DISPLAY_NAMES['auto'])

    return (
        booru.get('username', ''),
        booru.get('apikey', ''),
        booru.get('cookie', ''),
        active_name,
        active_name,
        booru.get('name', ''),
        booru.get('host', ''),
        system_display,
    )

def grabtags(url, negprompt, replacespaces, replaceunderscores, includeartist, includecharacter, includecopyright, includemeta):
    """Get the tags for the selected post and update all the relevant textboxes on the Select tab.

    Args:
        url (str): Either the full path to the post, or just the posts' id, formatted like "id:xxxxxx"
        negprompt (str): A negative prompt to paste into the relevant field. Setting to None will delete the existing negative prompt at the target
        replacespaces (bool): True to replace all the spaces in the tag list with ", "
        replaceunderscores (bool): True to replace the underscores in each tag with a space
        includeartist (bool): True to include the artist tags in the final tag string
        includecharacter (bool): True to include the character tags in the final tag string
        includecopyright (bool): True to include the copyright tags in the final tag string
        includemeta (bool): True to include the meta tags in the final tags string

    Returns:
        (str, str, str, str, str, str): A bunch of strings that will update some gradio components.
        In order, it's the final tag string, the local path to the saved image, the artist tags, the
        character tags, the copyright tags, and the meta tags.
    """
    if not isinstance(url, str):
        return

    host = gethost()
    username, apikey = getauth()
    cookie = getcookie()
    booru = _get_active_booru() or {}
    system_override = (booru.get("system", "auto") or "auto").lower()
    if system_override not in SUPPORTED_SYSTEMS:
        system_override = "auto"
    if system_override == "auto":
        booru_type = detect_booru_type(host, username, apikey, cookie)
    else:
        booru_type = system_override

    post_id, reference_url = _extract_post_id(url, host)
    fetcher = POST_FETCHERS.get(booru_type)
    if fetcher is None:
        raise gr.Error(f"Loading posts is not supported for booru type '{booru_type}'.")

    normalized = fetcher(host, username, apikey, cookie, post_id, reference_url)

    image_url = _absolute_url(host, normalized.get("image_url"))
    if not image_url:
        raise gr.Error("The selected post did not include an image URL.")

    artisttags = " ".join(normalized.get("artist", []))
    charactertags = " ".join(normalized.get("character", []))
    copyrighttags = " ".join(normalized.get("copyright", []))
    metatags = " ".join(normalized.get("meta", []))
    generaltags = " ".join(normalized.get("general", []))

    tag_sections = []
    if includeartist and artisttags:
        tag_sections.append(artisttags)
    if includecharacter and charactertags:
        tag_sections.append(charactertags)
    if includecopyright and copyrighttags:
        tag_sections.append(copyrighttags)
    if includemeta and metatags:
        tag_sections.append(metatags)
    if generaltags:
        tag_sections.append(generaltags)

    tags = " ".join(section for section in tag_sections if section)

    if replacespaces:
        tags = tags.replace(" ", ", ")
    if replaceunderscores:
        tags = tags.replace("_", " ")

    if negprompt:
        tags += f"\nNegative prompt: {negprompt}"

    temp_dir = os.path.join(edirectory, "tempimages")
    os.makedirs(temp_dir, exist_ok=True)
    savepath = os.path.join(temp_dir, "temp.jpg")
    headers = _build_request_headers(username, apikey, cookie, auth_mode=booru_type)
    _download_to_path(image_url, savepath, headers=headers)

    return (tags, savepath, artisttags, charactertags, copyrighttags, metatags)

def on_ui_tabs():
    #Just setting up some gradio components way early
    #For the most part, I've created each component at the place where it will be rendered
    #However, for these ones, I need to reference them before they would've otherwise been
    #initialized, so I put them up here instead. This is totally fine, since they can be 
    #rendered in the appropirate place with .render()
    _ensure_active()
    boorulist = _booru_names()
    active_booru = next((b for b in settings["boorus"] if b["name"] == settings["active"]), {})
    active_system_display = SYSTEM_DISPLAY_NAMES.get(active_booru.get("system", "auto"), SYSTEM_DISPLAY_NAMES["auto"])
    selectimage = gr.Image(label="Image", type="filepath", interactive=False)
    searchimages = gr.Gallery(label="Search Results", columns=3)
    activeboorutext1 = gr.Textbox(label="Current Booru", value=settings['active'], interactive=False)
    activeboorutext2 = gr.Textbox(label="Current Booru", value=settings['active'], interactive=False)
    curpage = gr.Textbox(value="1", label="Page Number", interactive=False, show_label=True)
    negprompt = gr.Textbox(label="Negative Prompt", value=settings['negativeprompt'], placeholder="Negative prompt to send with along with each prompt")

    with gr.Blocks() as interface:
        with gr.Tab("Select"):
            with gr.Row(equal_height=True):
                with gr.Column():
                    activeboorutext1.render()
                    #Go to that link, I dare you
                    imagelink = gr.Textbox(label="Link to image page", elem_id="selectbox", placeholder="https://danbooru.donmai.us/posts/4861569 or id:4861569")

                    with gr.Row():
                        selectedtags_artist = gr.Textbox(label="Artist Tags", interactive=False)
                        includeartist = gr.Checkbox(value=True, label="Include artist tags in tag string", interactive=True)
                    with gr.Row():
                        selectedtags_character = gr.Textbox(label="Character Tags", interactive=False)
                        includecharacter = gr.Checkbox(value=True, label="Include character tags in tag string", interactive=True)
                    with gr.Row():
                        selectedtags_copyright = gr.Textbox(label="Copyright Tags", interactive=False)
                        includecopyright = gr.Checkbox(value=True, label="Include copyright tags in tag string", interactive=True)
                    with gr.Row():
                        selectedtags_meta = gr.Textbox(label="Meta Tags", interactive=False)
                        includemeta = gr.Checkbox(value=False, label="Include meta tags in tag string", interactive=True)

                    selectedtags = gr.Textbox(label="Image Tags", interactive=False, lines=3)

                    replacespaces = gr.Checkbox(value=True, label="Replace spaces with a comma and a space", interactive=True)
                    replaceunderscores = gr.Checkbox(value=False, label="Replace underscores with spaces")

                    selectbutton = gr.Button(value="Select Image", variant="primary")
                    selectbutton.click(fn=grabtags,
                        inputs=
                            [imagelink, 
                            negprompt,
                            replacespaces, 
                            replaceunderscores,
                            includeartist, 
                            includecharacter, 
                            includecopyright, 
                            includemeta], 
                        outputs=
                            [selectedtags, 
                            selectimage, 
                            selectedtags_artist, 
                            selectedtags_character, 
                            selectedtags_copyright, 
                            selectedtags_meta])

                    clearselected = gr.Button(value="Clear")
                    #This is just a cheeky way to clear out all the components in this tab. I'm sure this is not what you're meant to use lambda functions for.
                    clearselected.click(fn=lambda: (None, None, None, None, None, None, None), outputs=[selectimage, selectedtags, selectedtags_artist, selectedtags_character, selectedtags_copyright, selectedtags_meta, imagelink])
                with gr.Column():
                    selectimage.render()
                    with gr.Row(equal_height=True):
                        #Don't even ask me how this works. I spent like three days reading generation_parameters_copypaste.py
                        #and I still don't quite know. Automatic1111 must've been high when he wrote that.
                        sendselected = modules.infotext_utils.create_buttons(["txt2img", "img2img", "inpaint", "extras"])
                        modules.infotext_utils.bind_buttons(sendselected, selectimage, selectedtags)
        with gr.Tab("Search"):
            with gr.Row(equal_height=True):
                with gr.Column():
                    activeboorutext2.render()
                    searchtext = gr.Textbox(label="Search string", placeholder="List of tags, delimited by spaces")
                    removeanimated = gr.Checkbox(label="Remove results with the \"animated\" tag", value=True)
                    searchbutton = gr.Button(value="Search Booru", variant="primary")
                    searchtext.submit(fn=searchbooru, inputs=[searchtext, removeanimated, curpage], outputs=[searchimages, curpage])
                    searchbutton.click(fn=searchbooru, inputs=[searchtext, removeanimated, curpage], outputs=[searchimages, curpage])
                with gr.Column():
                    with gr.Row():
                        prevpage = gr.Button(value="Previous Page")
                        curpage.render()
                        nextpage = gr.Button(value="Next Page")
                        #The functions called here will then call searchbooru, just with a page in/decrement modifier
                        prevpage.click(fn=gotoprevpage, inputs=[searchtext, removeanimated, curpage], outputs=[searchimages, curpage])
                        nextpage.click(fn=gotonextpage, inputs=[searchtext, removeanimated, curpage], outputs=[searchimages, curpage])
                    searchimages.render()
                    with gr.Row():
                        sendsearched = gr.Button(value="Send image to tag selection", elem_id="sendselected")
                        #In this particular instance, the javascript function will be used to read the page, find the selected image in
                        #gallery, and send it back here to the imagelink output. I cannot fathom why Gradio galleries can't
                        #be used as inputs, but so be it.
                        sendsearched.click(fn = None, _js="switch_to_select", outputs = imagelink)
        with gr.Tab("Settings/API Keys"):
            settingshelptext = gr.HTML(interactive=False, show_label = False, value="API info may not be necessary for some boorus, but certain information or posts may fail to load without it. For example, Danbooru doesn't show certain posts in search results unless you auth as a Gold tier member.")
            settingshelptext2 = gr.HTML(interactive=False, show_label=False, value="Also, please set the booru selection here before using select or search. If the booru presents a browser challenge, paste the validated session cookie below.")
            booru = gr.Dropdown(label="Booru", value=settings['active'], choices=boorulist, interactive=True)
            booruname = gr.Textbox(label="Booru Name", value=active_booru.get("name", settings.get("active", "")), placeholder="Display name shown in menus")
            booruhost = gr.Textbox(label="Booru Host URL", value=active_booru.get("host", ""), placeholder="https://example.com")
            u, a = getauth()
            username = gr.Textbox(label="Username", value=u)
            apikey = gr.Textbox(label="API Key", value=a)
            cookie = gr.Textbox(label="Session Cookie", value=active_booru.get("cookie", ""), placeholder="Optional raw Cookie header value", lines=2)
            boorutype = gr.Dropdown(label="Booru System", choices=list(SYSTEM_DISPLAY_NAMES.values()), value=active_system_display)
            negprompt.render()
            with gr.Row():
                addboorubutton = gr.Button(value="Add as New Booru", variant="secondary")
                savesettingsbutton = gr.Button(value="Save Booru", variant="primary")
                removeboorubutton = gr.Button(value="Remove Booru", variant="secondary")
            savesettingsbutton.click(fn=savesettings, inputs=[booru, booruname, booruhost, username, apikey, cookie, boorutype, negprompt], outputs=[booru, booruname, booruhost, username, apikey, cookie, boorutype, activeboorutext1, activeboorutext2])
            addboorubutton.click(fn=addbooru, inputs=[booruname, booruhost, username, apikey, cookie, boorutype, negprompt], outputs=[booru, booruname, booruhost, username, apikey, cookie, boorutype, activeboorutext1, activeboorutext2])
            removeboorubutton.click(fn=removebooru, inputs=[booru, negprompt], outputs=[booru, booruname, booruhost, username, apikey, cookie, boorutype, activeboorutext1, activeboorutext2])
            booru.change(fn=updatesettings, inputs=booru, outputs=[username, apikey, cookie, activeboorutext1, activeboorutext2, booruname, booruhost, boorutype])

    return (interface, "booru2prompt", "b2p_interface"),

script_callbacks.on_ui_tabs(on_ui_tabs)
