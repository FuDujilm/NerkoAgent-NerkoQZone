import os
import json
import asyncio
import time
import re
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import httpx

from src.common.logger import get_logger
from src.plugin_system.apis import config_api

logger = get_logger("Maizone.cookie")

# -----------------------------------
# Config access (lazy, no cyclic import)
# -----------------------------------
if TYPE_CHECKING:
    from .plugin import QzoneConfig  # type hints only


def _cfg() -> Optional["QzoneConfig"]:
    """Lazily fetch QzoneConfig from the plugin instance.
    Returns None if plugin/config is unavailable (e.g., in tests).
    """
    try:
        from .plugin import plugin, QzoneConfig
        return plugin.get_config(QzoneConfig)
    except Exception:
        return None


def _resolve_napcat_params(host: str, port: str, token: str) -> tuple[str, str, str]:
    """Resolve Napcat host/port/token with priority:
    1) Explicit args (if different from defaults)
    2) Environment variables: NAPCAT_HOST / NAPCAT_PORT / NAPCAT_TOKEN
    3) Plugin config (QzoneConfig): plugin_http_host / plugin_http_port / plugin_napcat_token
    4) Defaults ('127.0.0.1', '9999', '')
    """
    # 2) env fallback if arg still default-ish
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


# -----------------------------------
# Paths & state
# -----------------------------------

def get_cookie_file_path(uin: str) -> str:
    """Build cookie file path for given uin."""
    uin = str(uin).lstrip("0")
    base_dir = Path(__file__).parent.resolve()
    return str(base_dir / f"cookies-{uin}.json")


# QR login debounce (avoid re-scanning too often)
_last_qr_login_time = 0.0
# Cookie refresh throttle (avoid hammering providers)
_last_cookie_refresh_time = 0.0

qrcode_path = str(Path(__file__).parent.resolve() / "qrcode.png")


def should_skip_qr_login() -> bool:
    """Skip QR login if the last QR login was within 20 hours."""
    global _last_qr_login_time
    if _last_qr_login_time == 0:
        return False
    return (time.time() - _last_qr_login_time) < 20 * 3600


def update_last_qr_login_time():
    global _last_qr_login_time
    _last_qr_login_time = time.time()


# -----------------------------------
# Cookie helpers
# -----------------------------------

def parse_cookie_string(cookie_str: str) -> dict:
    """Parse a `name=value; name2=value2` cookie string into dict safely."""
    result: dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            result[k] = v
    return result


async def fetch_cookies_by_napcat(host: str, domain: str, port: str, napcat_token: str) -> dict:
    """Fetch cookies from a Napcat HTTP service.

    Expected response JSON shape:
    {"status": "ok", "data": {"cookies": "k=v; a=b"}}
    """
    url = f"http://{host}:{port}/get_cookies"
    max_retries = 1
    retry_delay = 1

    for attempt in range(max_retries):
        try:
            headers = {"Content-Type": "application/json"}
            if napcat_token:
                headers["Authorization"] = f"Bearer {napcat_token}"

            payload = {"domain": domain}

            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()

                if resp.status_code != 200:
                    error_msg = f"Napcat服务返回错误状态码: {resp.status_code}"
                    if resp.status_code == 403:
                        error_msg += " (Token验证失败)"
                    raise RuntimeError(error_msg)

                data = resp.json()
                if data.get("status") != "ok" or "cookies" not in data.get("data", {}):
                    raise RuntimeError(f"获取 cookie 失败: {data}")

                cookie_str = data["data"]["cookies"]
                parsed_cookies = parse_cookie_string(cookie_str)
                return parsed_cookies

        except httpx.RequestError as e:
            if attempt < max_retries - 1:
                logger.warning(
                    f"无法连接到Napcat服务(尝试 {attempt + 1}/{max_retries}): {url}，错误: {str(e)}"
                )
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
                continue
            logger.error(f"无法连接到Napcat服务(最终尝试): {url}，错误: {str(e)}")
            raise RuntimeError(f"无法连接到Napcat服务: {url}")
        except Exception as e:
            logger.error(f"获取cookie异常: {str(e)}")
            raise

    raise RuntimeError(f"无法连接到Napcat服务: 超过最大重试次数({max_retries})")


# -----------------------------------
# QR-code login
# -----------------------------------

# QQ 空间二维码登录相关 URL
qrcode_url = (
    "https://ssl.ptlogin2.qq.com/ptqrshow?appid=549000912&e=2&l=M&s=3&d=72&v=4&"
    "t=0.31232733520361844&daid=5&pt_3rd_aid=0"
)
login_check_url = (
    "https://xui.ptlogin2.qq.com/ssl/ptqrlogin?u1=https://qzs.qq.com/qzone/v5/loginsucc.html?para=izone&"
    "ptqrtoken={}&ptredirect=0&h=1&t=1&g=1&from_ui=1&ptlang=2052&action=0-0-1656992258324&js_ver=22070111&js_type=1&"
    "login_sig=&pt_uistyle=40&aid=549000912&daid=5&has_onekey=1&&o1vId=1e61428d61cb5015701ad73d5fb59f73"
)
check_sig_url = (
    "https://ptlogin2.qzone.qq.com/check_sig?pttype=1&uin={}&service=ptqrlogin&nodirect=1&ptsigx={}&"
    "s_url=https://qzs.qq.com/qzone/v5/loginsucc.html?para=izone&f_url=&ptlang=2052&ptredirect=100&aid=549000912&daid=5&"
    "j_later=0&low_login_hour=0&regmaster=0&pt_login_type=3&pt_aid=0&pt_aaid=16&pt_light=0&pt_3rd_aid=0"
)


class QzoneLogin:
    def getptqrtoken(self, qrsig: str) -> str:
        e = 0
        for ch in qrsig:
            e += (e << 5) + ord(ch)
        return str(2147483647 & e)

    async def login_via_qrcode(self, max_timeout_times: int = 3) -> dict:
        """Perform QR-code login, returning cookie dict on success."""
        for _ in range(max_timeout_times):
            async with httpx.AsyncClient() as client:
                req = await client.get(qrcode_url)
                qrsig = ""

                set_cookie_header = req.headers.get("Set-Cookie", "")
                for set_cookie in set_cookie_header.split(";"):
                    set_cookie = set_cookie.strip()
                    if set_cookie.startswith("qrsig="):
                        qrsig = set_cookie.split("=", 1)[1]
                        break
                if not qrsig:
                    raise Exception("qrsig is empty")

                ptqrtoken = self.getptqrtoken(qrsig)

                with open(qrcode_path, "wb") as f:
                    f.write(req.content)
                logger.info(f"二维码已保存于{qrcode_path}，请两分钟内使用手机QQ扫描登录")

                # wait for scan result (up to ~2 minutes)
                for _ in range(60):
                    await asyncio.sleep(2)
                    req = await client.get(login_check_url.format(ptqrtoken), cookies={"qrsig": qrsig})

                    if "二维码已失效" in req.text:
                        logger.info("二维码已失效，重新获取...")
                        break
                    if "登录成功" in req.text:
                        response_header_dict = req.headers

                        # extract redirect URL inside the callback
                        try:
                            # format: ptuiCB('0','0','<URL>','...')
                            m = re.search(r"ptuiCB\([^,]*,[^,]*,'([^']+)'", req.text)
                            url = m.group(1) if m else ""
                        except Exception:
                            url = ""

                        # ptsigx
                        m = re.search(r"ptsigx=([^&]+)&", url)
                        ptsigx = m.group(1) if m else ""
                        # uin
                        m = re.search(r"uin=(\d+)&", url)
                        uin = m.group(1) if m else ""

                        res = await client.get(
                            check_sig_url.format(uin, ptsigx),
                            cookies={"qrsig": qrsig},
                            headers={"Cookie": response_header_dict.get("Set-Cookie", "")},
                        )

                        final_cookie = res.headers.get("Set-Cookie", "")
                        final_cookie_dict: dict[str, str] = {}
                        for set_cookie in final_cookie.split(";, "):
                            for cookie in set_cookie.split(";"):
                                cookie = cookie.strip()
                                if not cookie or "=" not in cookie:
                                    continue
                                k, v = cookie.split("=", 1)
                                if k and k not in final_cookie_dict:
                                    final_cookie_dict[k] = v

                        if os.path.exists(qrcode_path):
                            os.remove(qrcode_path)

                        update_last_qr_login_time()
                        return final_cookie_dict

                    logger.debug("等待扫码登录...")

        raise Exception(f"{max_timeout_times}次尝试失败")


# -----------------------------------
# Alt method: clientkey
# -----------------------------------

async def fetch_cookies_by_clientkey() -> dict:
    """Fetch cookies via local clientkey service (QQ client must be running)."""
    uin = config_api.get_global_config("bot.qq_account", "")
    local_key_url = (
        "https://xui.ptlogin2.qq.com/cgi-bin-xlogin?appid=715021417&"
        "s_url=https%3A%2F%2Fhuifu.qq.com%2Findex.html"
    )
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(local_key_url, headers={"User-Agent": UA})
        pt_local_token = resp.cookies.get("pt_local_token", "")
        if not pt_local_token:
            raise Exception("无法获取pt_local_token")

        client_key_url = (
            "https://localhost.ptlogin2.qq.com:4301/pt_get_st?clientuin="
            f"{uin}&callback=ptui_getst_CB&r=0.7284667321181328&pt_local_tk={pt_local_token}"
        )
        resp = await client.get(
            client_key_url,
            headers={"User-Agent": UA, "Referer": "https://xui.ptlogin2.qq.com/"},
        )
        if resp.status_code == 400:
            raise Exception(f"获取clientkey失败: {resp.text}")

        clientkey = resp.cookies.get("clientkey", "")
        if not clientkey:
            raise Exception("无法获取clientkey")

        login_url = (
            "https://ssl.ptlogin2.qq.com/jump?ptlang=1033&clientuin="
            f"{uin}&clientkey={clientkey}&u1=https%3A%2F%2Fuser.qzone.qq.com%2F{uin}%2Finfocenter&keyindex=19"
        )

        resp = await client.get(login_url, headers={"User-Agent": UA}, follow_redirects=False)
        resp = await client.get(
            resp.headers["Location"],
            headers={"User-Agent": UA, "Referer": "https://ssl.ptlogin2.qq.com/"},
            cookies=resp.cookies,
            follow_redirects=False,
        )
        cookies = {cookie.name: cookie.value for cookie in resp.cookies.jar}
        return cookies


# -----------------------------------
# Main: renew cookies and persist
# -----------------------------------

async def renew_cookies(host: str = "127.0.0.1", port: str = "9999", napcat_token: str = "") -> None:
    """Try to refresh cookies and save to local file.

    Strategy:
      1) Try Napcat service (host/port/token)
      2) Fallback: clientkey
      3) Fallback: QR-code login (skipped if within 20h since last QR login)
      4) Fallback: read existing local cookie file

    Throttling: skip if refreshed within the past 1 hour.
    """
    global _last_cookie_refresh_time

    # Throttle frequent refresh
    now = time.time()
    if _last_cookie_refresh_time and (now - _last_cookie_refresh_time) < 1 * 3600:
        logger.info(
            f"上次更新cookie在{int(now - _last_cookie_refresh_time)}秒前，跳过更新cookie"
        )
        return

    # Resolve parameters
    host, port, napcat_token = _resolve_napcat_params(host, port, napcat_token)
    logger.info(
        f"renew_cookies: resolved host={host} port={port} token_set={bool(napcat_token)}"
    )

    # Target cookie file path
    uin = config_api.get_global_config("bot.qq_account", "")
    file_path = get_cookie_file_path(uin)
    directory = os.path.dirname(file_path)

    cookie_dict: Optional[dict] = None
    source = ""

    # 1) Napcat
    try:
        domain = "user.qzone.qq.com"
        cookie_dict = await fetch_cookies_by_napcat(host, domain, port, napcat_token)
        source = "napcat"
    except Exception as e:
        logger.error(f"Napcat获取cookie异常: {str(e)}。尝试通过ClientKey获取cookie")
        # 2) ClientKey
        try:
            cookie_dict = await fetch_cookies_by_clientkey()
            source = "clientkey"
        except Exception as e2:
            logger.error(f"ClientKey获取cookie异常: {str(e2)}")
            # 3) QR or local
            if should_skip_qr_login():
                logger.info("上次扫码登录在20小时内，跳过二维码登录，直接读取本地cookie")
                try:
                    if not os.path.exists(file_path):
                        raise FileNotFoundError(f"未找到本地cookie文件: {file_path}")
                    with open(file_path, "r", encoding="utf-8") as f:
                        cookie_dict = json.load(f)
                    source = "local"
                    logger.info("读取本地cookie文件")
                except FileNotFoundError as e3:
                    logger.error(f"本地cookie文件不存在: {str(e3)}")
                    raise RuntimeError("获取cookie失败")
            else:
                logger.info("尝试使用二维码登录")
                try:
                    login = QzoneLogin()
                    cookie_dict = await login.login_via_qrcode()
                    source = "qrcode"
                    logger.info("二维码登录成功")
                except Exception as e4:
                    logger.error(f"二维码登录失败: {str(e4)}，尝试读取本地cookie文件")
                    try:
                        if not os.path.exists(file_path):
                            raise FileNotFoundError(f"未找到本地cookie文件: {file_path}")
                        with open(file_path, "r", encoding="utf-8") as f:
                            cookie_dict = json.load(f)
                        source = "local"
                        logger.warning("读取本地cookie文件，可能cookie已过期")
                    except FileNotFoundError as e5:
                        logger.error(f"本地cookie文件不存在: {str(e5)}")
                        raise RuntimeError("获取cookie失败")

    # Persist
    if cookie_dict is None:
        raise RuntimeError("无法获取cookie")

    try:
        if not os.path.exists(directory):
            os.makedirs(directory)

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(cookie_dict, f, indent=4, ensure_ascii=False)
        logger.info(f"[OK] cookies 已保存至: {file_path} (source={source})")

        _last_cookie_refresh_time = time.time()
        # NOTE: QR login path already calls update_last_qr_login_time()
    except PermissionError as e:
        logger.error(f"文件写入权限不足: {str(e)}")
        raise
    except FileNotFoundError as e:
        logger.error(f"文件路径不存在: {str(e)}")
        raise
    except OSError as e:
        logger.error(f"文件写入失败: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"处理cookie时发生异常: {str(e)}")
        raise RuntimeError(f"处理cookie时发生异常: {str(e)}")


def qzone_live_post_llm(prompt: Optional[str] = None) -> str:
    """Legacy `/exec` wrapper forwarding to `plugin.qzone_live_post_llm`."""

    from .plugin import qzone_live_post_llm as _compat_live_post_llm

    return _compat_live_post_llm(prompt)
