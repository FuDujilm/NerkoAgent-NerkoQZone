import base64
import json
import os
import time
from typing import Any, Optional, TYPE_CHECKING

import httpx
import json5

from .cookie_manager import get_cookie_file_path, renew_cookies
from src.common.logger import get_logger
from src.plugin_system.apis import config_api
from src.chat.utils.utils_image import get_image_manager
from nekro_agent.api.plugin import dynamic_import_pkg

bs4 = dynamic_import_pkg("bs4")

logger = get_logger("Maizone.QzoneAPI")

# -----------------------------
# Helpers for config access
# -----------------------------
if TYPE_CHECKING:
    from .plugin import QzoneConfig  # only for type hints

def _cfg() -> Optional["QzoneConfig"]:
    """Lazily fetch QzoneConfig from the plugin instance.

    This avoids import-time cycles and always reflects the latest runtime
    configuration (hot-reload friendly). Returns None if unavailable.
    """
    try:
        from .plugin import plugin, QzoneConfig
        return plugin.get_config(QzoneConfig)
    except Exception:
        return None


def _resolve_napcat_params(host: str, port: str, token: str) -> tuple[str, str, str]:
    """Resolve Napcat host/port/token with a clear priority:

    1) Explicit function arguments (if different from defaults)
    2) Environment variables: NAPCAT_HOST / NAPCAT_PORT / NAPCAT_TOKEN
    3) Plugin config (QzoneConfig): plugin_http_host / plugin_http_port / plugin_napcat_token
    4) The original defaults
    """
    # 1) explicit args already provided via parameters

    # 2) env fallback only if args are still default-ish
    if host == "127.0.0.1":
        host = os.getenv("NAPCAT_HOST", host)
    if port == "9999":
        port = os.getenv("NAPCAT_PORT", port)
    if token == "":
        token = os.getenv("NAPCAT_TOKEN", token)

    # 3) plugin config fallback if still default-ish
    if host == "127.0.0.1" or port == "9999" or token == "":
        c = _cfg()
        if c:
            if host == "127.0.0.1":
                host = getattr(c, "plugin_http_host", host) or host
            if port == "9999":
                port = getattr(c, "plugin_http_port", port) or port
            if token == "":
                token = getattr(c, "plugin_napcat_token", token) or token

    return host, port, token


# -----------------------------
# Utility helpers
# -----------------------------

def generate_gtk(skey: str) -> str:
    """Compute Qzone gtk value from p_skey."""
    hash_val = 5381
    for ch in skey:
        hash_val += (hash_val << 5) + ord(ch)
    return str(hash_val & 2147483647)


def get_picbo_and_richval(upload_result: dict) -> tuple[str, str]:
    """Extract picbo and richval from image upload response for posting images."""
    json_data = upload_result
    if "ret" not in json_data:
        raise Exception("获取图片picbo和richval失败")
    if json_data["ret"] != 0:
        raise Exception("上传图片失败")

    picbo_spt = json_data["data"]["url"].split("&bo=")
    if len(picbo_spt) < 2:
        raise Exception("上传图片失败")
    picbo = picbo_spt[1]
    richval = ",{},{},{},{},{},{},,{},{}".format(
        json_data["data"]["albumid"],
        json_data["data"]["lloc"],
        json_data["data"]["sloc"],
        json_data["data"]["type"],
        json_data["data"]["height"],
        json_data["data"]["width"],
        json_data["data"]["height"],
        json_data["data"]["width"],
    )
    return picbo, richval


def extract_code_html(html_content: str) -> Any | None:
    """Extract `code` from Qzone HTML callback script blocks if present."""
    try:
        soup = bs4.BeautifulSoup(html_content, "html.parser")
        script_tags = soup.find_all("script")
        for script in script_tags:
            if script.string and "frameElement.callback" in script.string:
                script_content = script.string
                start_index = script_content.find("frameElement.callback(") + len("frameElement.callback(")
                end_index = script_content.rfind(");")
                if 0 < start_index < end_index:
                    json_str = script_content[start_index:end_index].strip()
                    if json_str.endswith(";"):
                        json_str = json_str[:-1]
                    data = json5.loads(json_str)
                    return data.get("code")
        return None
    except Exception:
        return None


def extract_code_json(json_response: Any) -> Any | None:
    """Extract `code` from JSON/JSON-string response if present."""
    try:
        if isinstance(json_response, str):
            data = json.loads(json_response)
        else:
            data = json_response
        return data.get("code", None)
    except (json.JSONDecodeError, KeyError, AttributeError):
        return None


def image_to_base64(image: bytes) -> str:
    """Encode raw image bytes to base64 string (without prefix)."""
    return base64.b64encode(image).decode("utf-8")


# -----------------------------
# Qzone API client
# -----------------------------

class QzoneAPI:
    # Qzone CGI endpoints
    UPLOAD_IMAGE_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
    EMOTION_PUBLISH_URL = (
        "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
    )
    DOLIKE_URL = (
        "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
    )
    COMMENT_URL = (
        "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    )
    REPLY_URL = (
        "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    )
    LIST_URL = (
        "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
    )
    ZONE_LIST_URL = (
        "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
    )

    def __init__(self, cookies_dict: dict = {}):
        self.cookies = cookies_dict
        self.gtk2 = ""
        self.uin = 0
        self.qq_nickname = ""

        # Prefer UIN from cookies; fallback to global config
        try:
            uin_val = None
            for k in ("uin", "p_uin", "pt2gguin"):
                if self.cookies.get(k):
                    uin_val = self.cookies.get(k)
                    break

            if uin_val:
                import re

                m = re.search(r"(\d+)", str(uin_val))
                if m:
                    self.uin = int(m.group(1))
                else:
                    try:
                        self.uin = int(str(uin_val))
                    except Exception:
                        self.uin = 0
            else:
                try:
                    cfg_uin = config_api.get_global_config("bot.qq_account", None)
                    self.uin = int(str(cfg_uin)) if cfg_uin else 0
                except Exception:
                    self.uin = 0
        except Exception:
            self.uin = 0

        if "p_skey" in self.cookies:
            try:
                self.gtk2 = generate_gtk(self.cookies["p_skey"])
            except Exception:
                self.gtk2 = ""

    async def do(
        self,
        method: str,
        url: str,
        params: dict = {},
        data: dict = {},
        headers: dict = {},
        cookies: Optional[dict] = None,
        timeout: int = 10,
    ) -> httpx.Response:
        """Send an async HTTP request with cookies and return the response."""
        if cookies is None:
            cookies = self.cookies

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.request(
                method=method,
                url=url,
                params=params,
                data=data,
                headers=headers,
                cookies=cookies,
            )
        return response

    async def get_image_base64_by_url(self, url: str) -> str:
        """Fetch an image from URL and return a base64 string."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Referer": "https://qzone.qq.com/",
        }
        async with httpx.AsyncClient(follow_redirects=True) as client:
            request = httpx.Request("GET", url, headers=headers)
            response = await client.send(request)

        if response.status_code != 200:
            logger.error(f"请求失败: {response.url}")
            logger.error(f"原始URL: {url}")
            raise Exception(f"图片请求失败: {response.status_code}")

        return base64.b64encode(response.content).decode("utf-8")

    async def upload_image(self, image: bytes) -> dict:
        """Upload an image to Qzone and return parsed JSON response as dict."""
        res = await self.do(
            method="POST",
            url=self.UPLOAD_IMAGE_URL,
            data={
                "filename": "filename",
                "zzpanelkey": "",
                "uploadtype": "1",
                "albumtype": "7",
                "exttype": "0",
                "skey": self.cookies["skey"],
                "zzpaneluin": self.uin,
                "p_uin": self.uin,
                "uin": self.uin,
                "p_skey": self.cookies["p_skey"],
                "output_type": "json",
                "qzonetoken": "",
                "refer": "shuoshuo",
                "charset": "utf-8",
                "output_charset": "utf-8",
                "upload_hd": "1",
                "hd_width": "2048",
                "hd_height": "10000",
                "hd_quality": "96",
                "backUrls": (
                    "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                    "http://119.147.64.75/cgi-bin/upload/cgi_upload_image"
                ),
                "url": "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk=" + self.gtk2,
                "base64": "1",
                "picfile": image_to_base64(image),
            },
            headers={
                "referer": "https://user.qzone.qq.com/" + str(self.uin),
                "origin": "https://user.qzone.qq.com",
            },
            timeout=60,
        )
        if res.status_code == 200:
            logger.debug(f"上传图片响应: {res.text}")
            # The response may contain a JSON object wrapped with extra text; strip it by locating braces
            payload_text = res.text
            start = payload_text.find("{")
            end = payload_text.rfind("}") + 1
            if start == -1 or end == 0:
                raise Exception("上传图片响应解析失败")
            return json.loads(payload_text[start:end])
        raise Exception("上传图片失败")

    async def publish_emotion(self, content: str, images: list[bytes] | None = None) -> str:
        """Publish a Qzone post with optional images and return the post tid."""
        if images is None:
            images = []

        post_data = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": content,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": self.uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": "https://user.qzone.qq.com/" + str(self.uin),
        }

        if images:
            pic_bos: list[str] = []
            richvals: list[str] = []
            for img in images:
                uploadresult = await self.upload_image(img)
                picbo, richval = get_picbo_and_richval(uploadresult)
                pic_bos.append(picbo)
                richvals.append(richval)
            post_data["pic_bo"] = ",".join(pic_bos)
            post_data["richtype"] = "1"
            post_data["richval"] = "\t".join(richvals)

        res = await self.do(
            method="POST",
            url=self.EMOTION_PUBLISH_URL,
            params={"g_tk": self.gtk2, "uin": self.uin},
            data=post_data,
            headers={
                "referer": "https://user.qzone.qq.com/" + str(self.uin),
                "origin": "https://user.qzone.qq.com",
            },
        )
        if res.status_code == 200:
            if extract_code_json(res.text) != 0:
                logger.error(f"发表说说失败，响应内容: {res.text}")
                raise Exception("发表说说失败: " + res.text)
            return res.json()["tid"]
        raise Exception("发表说说失败: " + res.text)

    async def like(self, fid: str, target_qq: str) -> bool:
        """Like a specific feed by fid and target QQ."""
        uin = self.uin
        post_data = {
            "qzreferrer": f"https://user.qzone.qq.com/{uin}",
            "opuin": uin,
            "unikey": f"http://user.qzone.qq.com/{target_qq}/mood/{fid}",
            "curkey": f"http://user.qzone.qq.com/{target_qq}/mood/{fid}",
            "appid": 311,
            "from": 1,
            "typeid": 0,
            "abstime": int(time.time()),
            "fid": fid,
            "active": 0,
            "format": "json",
            "fupdate": 1,
        }
        res = await self.do(
            method="POST",
            url=self.DOLIKE_URL,
            params={"g_tk": self.gtk2},
            data=post_data,
            headers={
                "referer": "https://user.qzone.qq.com/" + str(self.uin),
                "origin": "https://user.qzone.qq.com",
            },
        )
        if res.status_code == 200:
            if extract_code_json(res.text) != 0:
                logger.error("点赞失败" + res.text)
                return False
            return True
        raise Exception("点赞失败: " + res.text)

    async def comment(self, fid: str, target_qq: str, content: str) -> bool:
        """Comment on a specific feed."""
        uin = self.uin
        post_data = {
            "topicId": f"{target_qq}_{fid}__1",
            "uin": uin,
            "hostUin": target_qq,
            "feedsType": 100,
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "plat": "qzone",
            "source": "ic",
            "platformid": 52,
            "format": "fs",
            "ref": "feeds",
            "content": content,
        }
        res = await self.do(
            method="POST",
            url=self.COMMENT_URL,
            params={"g_tk": self.gtk2},
            data=post_data,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
                ),
                "referer": "https://user.qzone.qq.com/" + str(self.uin),
                "origin": "https://user.qzone.qq.com",
            },
        )
        if res.status_code == 200:
            if extract_code_html(res.text) != 0:
                logger.error("评论失败" + res.text)
                return False
            return True
        raise Exception("评论失败: " + res.text)

    async def reply(
        self,
        fid: str,
        target_qq: str,
        target_nickname: str,
        content: str,
        comment_tid: str,
    ) -> bool:
        """Reply to a specific comment (implemented via @nickname)."""
        uin = self.uin
        post_data = {
            "topicId": f"{uin}_{fid}__1",
            "uin": uin,
            "hostUin": uin,
            "content": f"回复@ {target_nickname} ：{content}",
            "format": "fs",
            "plat": "qzone",
            "source": "ic",
            "platformid": 52,
            "ref": "feeds",
            "richtype": "",
            "richval": "",
            "paramstr": f"@{target_nickname}",
        }
        res = await self.do(
            method="POST",
            url=self.REPLY_URL,
            params={"g_tk": self.gtk2},
            data=post_data,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
                )
            },
        )
        if res.status_code == 200:
            if extract_code_html(res.text) != 0:
                logger.error("回复失败" + res.text)
                return False
            return True
        raise Exception(f"回复失败，错误码: {res.status_code}")

    async def get_list(self, target_qq: str, num: int) -> list[dict[str, Any]]:
        """Get feed list for target_qq and return unread feed items."""
        logger.info(f"即将获取 {target_qq} 的说说列表...")
        res = await self.do(
            method="GET",
            url=self.LIST_URL,
            params={
                "g_tk": self.gtk2,
                "uin": target_qq,
                "ftype": 0,
                "sort": 0,
                "pos": 0,
                "num": num,
                "replynum": 100,
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "jsonp",
                "need_comment": 1,
                "need_private_comment": 1,
            },
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                ),
                "Referer": f"https://user.qzone.qq.com/{target_qq}",
                "Host": "user.qzone.qq.com",
                "Connection": "keep-alive",
            },
        )

        if res.status_code != 200:
            raise Exception("访问失败: " + str(res.status_code))

        data = res.text
        if data.startswith("_preloadCallback(") and data.endswith(");"):
            json_str = data[len("_preloadCallback(") : -2]
        else:
            json_str = data

        try:
            json_data = json.loads(json_str)
            logger.debug(f"原始说说数据: {json_data}")
            uin_nickname = json_data.get("logininfo").get("name")
            self.qq_nickname = uin_nickname

            if json_data.get("code") != 0:
                return [{"error": json_data.get("message")}] 

            feeds_list: list[dict[str, Any]] = []
            msglist = json_data.get("msglist") or []
            if not msglist:
                logger.warning("msglist为空或None，返回空的说说列表")

            for msg in msglist:
                # Skip if already commented by current user (and not own post)
                is_comment = False
                if isinstance(msg.get("commentlist"), list):
                    for comment in msg.get("commentlist"):
                        if uin_nickname == comment.get("name") and target_qq != str(self.uin):
                            logger.info("已评论过此说说，即将跳过")
                            is_comment = True
                            break
                if is_comment:
                    continue

                timestamp = msg.get("created_time", "")
                if timestamp:
                    created_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
                else:
                    created_time = msg.get("createTime", "unknown")
                tid = msg.get("tid", "")
                content = msg.get("content", "")
                logger.info(f"正在阅读说说内容: {content[:20]}...")

                # Images -> describe via image manager
                images: list[str] = []
                image_manager = get_image_manager()

                async def append_image_description(url: str):
                    if not url:
                        return
                    try:
                        image_base64 = await self.get_image_base64_by_url(url)
                        image_description = await image_manager.get_image_description(image_base64)
                        images.append(image_description)
                    except Exception as img_err:
                        logger.warning(f"获取图片描述失败: {img_err}")

                for pic in (msg.get("pic") or []):
                    url = pic.get("url1") or pic.get("pic_id") or pic.get("smallurl")
                    await append_image_description(url)

                # video thumbnails treated as images
                for video in (msg.get("video") or []):
                    video_image_url = video.get("url1") or video.get("pic_url")
                    await append_image_description(video_image_url)

                # Extract video play URLs
                videos: list[str] = []
                for video in (msg.get("video") or []):
                    url = video.get("url3")
                    if url:
                        videos.append(url)

                # Repost content
                rt_con = ""
                rt_data = msg.get("rt_con") or {}
                if isinstance(rt_data, dict):
                    rt_con = rt_data.get("content", "")

                # Comments
                def _safe_int(value):
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        return None

                comments: list[dict[str, Any]] = []
                for comment in (msg.get("commentlist") or []):
                    comment_nickname = comment.get("name", "")
                    comment_content = comment.get("content", "")
                    comment_uin = comment.get("uin", "")
                    comment_tid_value = _safe_int(comment.get("tid"))
                    comment_time = comment.get("createTime", "") or comment.get("createTime2", "")

                    for sub_comment in (comment.get("list_3") or []):
                        sub_content = sub_comment.get("content", "")
                        sub_nickname = sub_comment.get("name", "")
                        sub_uin = sub_comment.get("uin", "")
                        sub_tid_value = _safe_int(sub_comment.get("tid"))
                        sub_time = sub_comment.get("createTime", "") or comment.get("createTime2", "")
                        sub_parent = comment_tid_value
                        comments.append(
                            {
                                "content": sub_content,
                                "qq_account": str(sub_uin),
                                "nickname": sub_nickname,
                                "comment_tid": sub_tid_value,
                                "created_time": sub_time,
                                "parent_tid": sub_parent,
                            }
                        )

                    comments.append(
                        {
                            "content": comment_content,
                            "qq_account": str(comment_uin),
                            "nickname": comment_nickname,
                            "comment_tid": comment_tid_value,
                            "created_time": comment_time,
                            "parent_tid": None,
                        }
                    )

                feeds_list.append(
                    {
                        "target_qq": str(target_qq),
                        "tid": str(tid),
                        "created_time": created_time,
                        "content": content,
                        "images": images,
                        "videos": videos,
                        "rt_con": rt_con,
                        "comments": comments,
                    }
                )

            if not feeds_list:
                return [{"error": "你已经看过最近的所有说说了，没有必要再看一遍"}]
            return feeds_list

        except Exception as e:
            logger.error(str(json_str))
            return [{"error": f"{e},你没有看到任何东西"}]

    async def monitor_get_list(self, self_readnum: int) -> list[dict[str, Any]]:
        """Get timeline (including self), parse HTML, return items + merge self comments."""
        res = await self.do(
            method="GET",
            url=self.ZONE_LIST_URL,
            params={
                "uin": self.uin,
                "scope": 0,
                "view": 1,
                "filter": "all",
                "flag": 1,
                "applist": "all",
                "pagenum": 1,
                "aisortEndTime": 0,
                "aisortOffset": 0,
                "aisortBeginTime": 0,
                "begintime": 0,
                "format": "json",
                "g_tk": self.gtk2,
                "useutf8": 1,
                "outputhtmlfeed": 1,
            },
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                ),
                "Referer": f"https://user.qzone.qq.com/{self.uin}",
                "Host": "user.qzone.qq.com",
                "Connection": "keep-alive",
            },
        )

        if res.status_code != 200:
            raise Exception("访问失败: " + str(res.status_code))

        data_text = res.text
        if data_text.startswith("_Callback(") and data_text.endswith(");"):
            data_text = data_text[len("_Callback(") : -2]
        data_text = data_text.replace("undefined", "null")

        try:
            data = json5.loads(data_text)["data"]["data"]
        except Exception as e:
            logger.error(f"解析错误: {e}")
            data = []

        try:
            feeds_list: list[dict[str, Any]] = []
            num_self = 0
            for feed in data:
                if not feed:
                    continue
                # Only mood appid=311
                appid = str(feed.get("appid", ""))
                if appid != "311":
                    continue

                target_qq = feed.get("uin", "")
                if target_qq == str(self.uin):
                    num_self += 1
                tid = feed.get("key", "")
                if not target_qq or not tid:
                    logger.error(f"无效的说说数据: target_qq={target_qq}, tid={tid}")
                    continue

                html_content = feed.get("html", "")
                if not html_content:
                    logger.error(f"说说内容为空: UIN={target_qq}, TID={tid}")
                    continue

                soup = bs4.BeautifulSoup(html_content, "html.parser")

                # main text
                text_div = soup.find("div", class_="f-info")
                text = text_div.get_text(strip=True) if text_div else ""

                # repost content
                rt_con = ""
                txt_box = soup.select_one("div.txt-box")
                if txt_box:
                    rt_con = txt_box.get_text(strip=True)
                    if "：" in rt_con:
                        rt_con = rt_con.split("：", 1)[1].strip()

                # images
                image_urls: list[str] = []
                img_box = soup.find("div", class_="img-box")
                if img_box:
                    for img in img_box.find_all("img"):
                        src = img.get("src")
                        if src and not src.startswith("http://qzonestyle.gtimg.cn"):
                            image_urls.append(src)
                # video thumbnail
                img_tag = soup.select_one("div.video-img img")
                if img_tag and "src" in img_tag.attrs:
                    image_urls.append(img_tag["src"])
                unique_urls = list(set(image_urls))

                images: list[str] = []
                for url in unique_urls:
                    try:
                        image_base64 = await self.get_image_base64_by_url(url)
                        image_manager = get_image_manager()
                        description = await image_manager.get_image_description(image_base64)
                        images.append(description)
                    except Exception as e:
                        logger.info(f"图片识别失败: {url} - {str(e)}")

                # videos
                videos: list[str] = []
                video_div = soup.select_one("div.img-box.f-video-wrap.play")
                if video_div and "url3" in video_div.attrs:
                    videos.append(video_div["url3"])

                # comments
                comments_list: list[dict[str, Any]] = []
                comment_items = soup.select("li.comments-item.bor3")
                if comment_items:
                    for item in comment_items:
                        qq_account = item.get("data-uin", "")
                        comment_tid = item.get("data-tid", "")
                        nickname = item.get("data-nick", "")

                        content_div = item.select_one("div.comments-content")
                        if content_div:
                            for op in content_div.select("div.comments-op"):
                                op.decompose()
                            content = content_div.get_text(" ", strip=True)
                        else:
                            content = ""

                        parent_tid = None
                        parent_div = item.find_parent("div", class_="mod-comments-sub")
                        if parent_div:
                            parent_li = parent_div.find_parent("li", class_="comments-item")
                            if parent_li:
                                parent_tid = parent_li.get("data-tid")

                        comments_list.append(
                            {
                                "qq_account": str(qq_account),
                                "nickname": nickname,
                                "comment_tid": int(comment_tid) if comment_tid else None,
                                "content": content,
                                "parent_tid": None if parent_tid is None else int(parent_tid),
                            }
                        )

                feeds_list.append(
                    {
                        "target_qq": str(target_qq),
                        "tid": str(tid),
                        "content": text,
                        "images": images,
                        "videos": videos,
                        "rt_con": rt_con,
                        "comments": comments_list,
                    }
                )

            logger.info(f"成功解析 {len(feeds_list)} 条最新说说，其中自己的说说有 {num_self} 条")

            # drop own feeds; merge full comments from detail API for self
            feeds_list = [item for item in feeds_list if item.get("target_qq") != str(self.uin)]
            self_feeds = await self.get_list(str(self.uin), self_readnum)
            feeds_list.extend(self_feeds)
            return feeds_list
        except Exception as e:
            logger.error(f"解析说说错误：{str(e)}", exc_info=True)
            return []

    async def get_send_history(self, num: int) -> str:
        """Build a prompt-like string from your own recent posts."""
        feeds_list = await self.get_list(target_qq=str(self.uin), num=num)
        history = "==================="
        for feed in feeds_list:
            if not feed.get("rt_con", ""):
                history += f"""
                    时间：'{feed.get("created_time", "")}'。
                    说说内容：'{feed.get("content", "")}'
                    图片：'{feed.get("images", [])}'
                    ===================
                    """
            else:
                history += f"""
                    时间: '{feed.get("created_time", "")}'。
                    转发了一条说说，内容为: '{feed.get("rt_con", "")}'
                    图片: '{feed.get("images", [])}'
                    对该说说的评论为: '{feed.get("content", "")}'
                    ===================
                    """
        return history


# -----------------------------
# Factories
# -----------------------------

def create_qzone_api() -> QzoneAPI | None:
    """Create QzoneAPI from local cookie file bound to bot.qq_account.

    Returns None if cookie file is missing or invalid.
    """
    qq_account = config_api.get_global_config("bot.qq_account", "")
    cookie_file = get_cookie_file_path(qq_account)

    cookies = None
    if os.path.exists(cookie_file):
        try:
            with open(cookie_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except Exception as e:
            logger.error(f"读取 cookie 文件失败: {cookie_file}，错误: {e}")
            cookies = None
    else:
        logger.error(f"cookie 文件不存在: {cookie_file}")

    if cookies:
        return QzoneAPI(cookies)
    return None


async def ensure_qzone_api(
    host: str = "127.0.0.1",
    port: str = "9999",
    napcat_token: str = "",
) -> QzoneAPI | None:
    """Ensure a working QzoneAPI instance.

    Steps:
    1) Resolve Napcat params (args > env > plugin config > defaults)
    2) Attempt to renew cookies with these params (warning on failure)
    3) Create QzoneAPI from local cookie file
    """
    host, port, napcat_token = _resolve_napcat_params(host, port, napcat_token)
    logger.info(
        f"ensure_qzone_api: resolved host={host} port={port} token_set={bool(napcat_token)}"
    )

    try:
        await renew_cookies(host, port, napcat_token)
    except Exception as e:
        logger.warning(f"ensure_qzone_api: renew_cookies 失败: {e}")

    qzone = create_qzone_api()
    if qzone is None:
        logger.error("ensure_qzone_api: 无法创建 QzoneAPI（cookie 缺失或解析失败）")
    return qzone
