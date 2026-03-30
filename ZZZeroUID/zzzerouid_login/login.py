import asyncio
import hashlib
import time
from collections import OrderedDict
from typing import Optional

from async_timeout import timeout
from gsuid_core.bot import Bot
from gsuid_core.config import core_config
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.utils.cookie_manager.add_ck import _deal_ck
from gsuid_core.web_app import app
from pydantic import BaseModel
from starlette.responses import FileResponse, HTMLResponse


# ============ TimedCache ============

class TimedCache:
    def __init__(self, timeout=5, maxsize=10):
        self.cache = OrderedDict()
        self.timeout = timeout
        self.maxsize = maxsize

    def set(self, key, value):
        if len(self.cache) >= self.maxsize:
            self._clean_up()
        if key in self.cache:
            self.cache.move_to_end(key)
        else:
            self._clean_up()
        self.cache[key] = (value, time.time() + self.timeout)

    def get(self, key):
        if key in self.cache:
            value, expiry = self.cache.pop(key)
            if time.time() < expiry:
                self.cache[key] = (value, expiry)
                return value
        return None

    def delete(self, key):
        if key in self.cache:
            del self.cache[key]

    def _clean_up(self):
        current_time = time.time()
        keys_to_delete = [
            key
            for key, (_, expiry_time) in self.cache.items()
            if expiry_time <= current_time
        ]
        for key in keys_to_delete:
            del self.cache[key]


cache = TimedCache(timeout=600, maxsize=10)

GAME_TITLE = "[绝区零]"
TEMPLATES_DIR = (
    __import__("pathlib").Path(__file__).parent.parent / "templates"
)


# ============ Pydantic Models ============

class AutoLoginModel(BaseModel):
    auth: str
    email: str
    password: str
    geetest_data: Optional[str] = None


class CookieLoginModel(BaseModel):
    auth: str
    cookie: str


# ============ Utility ============

def get_token(user_id: str) -> str:
    # Use a different salt so zzz and sr tokens don't collide
    return hashlib.sha256(f"zzz_{user_id}".encode()).hexdigest()[:8]


def get_server_url() -> str:
    host = core_config.get_config("HOST")
    port = core_config.get_config("PORT")
    if host in ("localhost", "127.0.0.1", "0.0.0.0"):
        host = "localhost"
    return f"http://{host}:{port}"


# ============ Bot Command ============

async def page_login(bot: Bot, ev: Event):
    at_sender = bool(ev.group_id)
    user_token = get_token(ev.user_id)

    # Prevent duplicate sessions
    existing = cache.get(user_token)
    if isinstance(existing, dict):
        url = get_server_url()
        return await bot.send(
            f"{GAME_TITLE} 您已有一个登录会话进行中\n"
            f"请在浏览器中打开: {url}/zzz/login/{user_token}\n"
            f"登录地址10分钟内有效",
            at_sender=at_sender,
        )

    data = {
        "user_id": ev.user_id,
        "bot_id": ev.bot_id,
        "group_id": ev.group_id,
        "status": "waiting",
        "result_msg": None,
        "cookie_str": None,
    }
    cache.set(user_token, data)

    url = get_server_url()
    login_url = f"{url}/zzz/login/{user_token}"

    await bot.send(
        f"{GAME_TITLE} 您的id为【{ev.user_id}】\n"
        f"请复制地址到浏览器打开\n"
        f" {login_url}\n"
        f"登录地址10分钟内有效",
        at_sender=at_sender,
    )

    # Poll for login completion
    try:
        async with timeout(600):
            while True:
                result = cache.get(user_token)
                if result is None:
                    return await bot.send(
                        f"{GAME_TITLE} 登录超时!",
                        at_sender=at_sender,
                    )

                if result.get("status") == "success":
                    msg = result.get("result_msg", "登录成功!")
                    return await bot.send(
                        f"{GAME_TITLE} {msg}",
                        at_sender=at_sender,
                    )
                elif result.get("status") == "error":
                    msg = result.get("result_msg", "登录失败!")
                    return await bot.send(
                        f"{GAME_TITLE} {msg}",
                        at_sender=at_sender,
                    )

                await asyncio.sleep(1)
    except asyncio.TimeoutError:
        cache.delete(user_token)
        return await bot.send(
            f"{GAME_TITLE} 登录超时!",
            at_sender=at_sender,
        )


# ============ Process cookies via gsuid_core ============

# ZZZ UIDs are 10+ digits; region determined by prefix
# ZZZ region mapping (matches the plugin's own REGION_MAP in request.py)
ZZZ_SERVER = {
    "10": "prod_gf_us",
    "13": "prod_gf_jp",
    "15": "prod_gf_eu",
    "17": "prod_gf_sg",
}


def _extract_account_id(cookie_str: str) -> str:
    """Extract account_id from a cookie string."""
    from http.cookies import SimpleCookie

    simp = SimpleCookie(cookie_str)
    for key in ["account_id_v2", "account_id", "ltuid_v2", "ltuid", "stuid"]:
        if key in simp:
            return simp[key].value
    return ""


async def _post_process_cookie(user_id: str, bot_id: str, cookie_str: str):
    """Fix international user issues after _deal_ck() runs.

    _deal_ck() has two problems for international HoYoLab users:
    1. Stores only account_id+cookie_token, losing ltoken_v2 etc.
    2. Sets zzz_uid=None because get_mihoyo_bbs_info() returns empty.

    This function uses direct SQL to reliably fix both, targeting
    the specific gsuser row by mys_id to avoid clobbering multi-account setups.
    """
    from gsuid_core.utils.database.base_models import DB_PATH

    import sqlite3

    mys_id = _extract_account_id(cookie_str)
    if not mys_id:
        logger.warning("[zzz登录] Could not extract account_id from cookie")
        return

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    try:
        # 1) Patch cookie with full string
        cursor.execute(
            "UPDATE gsuser SET cookie = ? WHERE mys_id = ?",
            (cookie_str, mys_id),
        )
        logger.info(f"[zzz登录] Patched cookie for mys_id={mys_id}")

        # 2) Link zzz_uid if missing
        cursor.execute(
            "SELECT zzz_uid FROM gsuser WHERE mys_id = ?", (mys_id,)
        )
        row = cursor.fetchone()
        if row and not row[0]:
            # zzz_uid is NULL — find an unlinked one from gsbind
            cursor.execute(
                "SELECT zzz_uid FROM gsbind WHERE user_id = ? AND bot_id = ?",
                (user_id, bot_id),
            )
            bind_row = cursor.fetchone()
            if bind_row and bind_row[0]:
                zzz_uids = bind_row[0].split("_")
                # Find a zzz_uid not already claimed by another gsuser row
                for zzz_uid in zzz_uids:
                    cursor.execute(
                        "SELECT id FROM gsuser WHERE zzz_uid = ?", (zzz_uid,)
                    )
                    if not cursor.fetchone():
                        region = ZZZ_SERVER.get(
                            zzz_uid[:2], "prod_official_usa"
                        )
                        cursor.execute(
                            "UPDATE gsuser SET zzz_uid = ?, zzz_region = ? WHERE mys_id = ?",
                            (zzz_uid, region, mys_id),
                        )
                        logger.info(
                            f"[zzz登录] Linked zzz_uid={zzz_uid} to mys_id={mys_id}"
                        )
                        break

        conn.commit()
    except Exception as e:
        logger.error(f"[zzz登录] post-process failed: {e}")
    finally:
        conn.close()


async def process_cookie(auth: str, cookie_str: str) -> dict:
    """Process a cookie string through gsuid_core's _deal_ck."""
    entry = cache.get(auth)
    if entry is None:
        return {"success": False, "msg": "会话已过期"}

    user_id = entry["user_id"]
    bot_id = entry["bot_id"]

    try:
        result_msg = await _deal_ck(bot_id, cookie_str, user_id)
        ok_count = result_msg.count("成功")

        if ok_count >= 1:
            # Fix for international users: patch cookie + link zzz_uid
            await _post_process_cookie(user_id, bot_id, cookie_str)

            entry["status"] = "success"
            entry["result_msg"] = (
                "Cookie绑定成功!\n"
                "使用【zzz绑定uid】绑定你的UID\n"
                "使用【zzz查询】查看角色面板"
            )
        else:
            entry["status"] = "error"
            entry["result_msg"] = f"Cookie绑定失败: {result_msg}"

        cache.set(auth, entry)
        return {
            "success": ok_count >= 1,
            "msg": entry["result_msg"],
        }
    except Exception as e:
        logger.error(f"[zzz登录] Cookie处理失败: {e}")
        entry["status"] = "error"
        entry["result_msg"] = f"处理失败: {str(e)}"
        cache.set(auth, entry)
        return {"success": False, "msg": str(e)}


# ============ FastAPI Endpoints ============

@app.get("/zzz/cookie-guide")
async def zzz_cookie_guide():
    pdf_path = TEMPLATES_DIR / "HoYoLab_Cookie_Guide.pdf"
    return FileResponse(pdf_path, media_type="application/pdf")


@app.get("/zzz/login/{auth}")
async def zzz_login_index(auth: str):
    temp = cache.get(auth)
    if temp is None:
        html_path = TEMPLATES_DIR / "zzz_404.html"
        html = html_path.read_text(encoding="utf-8")
        return HTMLResponse(html)

    url = get_server_url()
    html_path = TEMPLATES_DIR / "zzz_login.html"
    html_template = html_path.read_text(encoding="utf-8")

    html = html_template.replace("{{ server_url }}", url)
    html = html.replace("{{ auth }}", auth)
    html = html.replace("{{ userId }}", str(temp.get("user_id", "")))

    return HTMLResponse(html)


@app.post("/zzz/login/cookie")
async def zzz_login_cookie(data: CookieLoginModel):
    """Manual cookie paste endpoint."""
    entry = cache.get(data.auth)
    if entry is None:
        return {"success": False, "msg": "会话已过期，请重新发送登录命令"}

    cookie_str = data.cookie.strip()
    if not cookie_str:
        return {"success": False, "msg": "Cookie不能为空"}

    known_keys = [
        "ltoken_v2", "cookie_token_v2", "cookie_token",
        "stoken", "stoken_v2", "login_ticket", "ltoken",
    ]
    if not any(k in cookie_str for k in known_keys):
        return {
            "success": False,
            "msg": "Cookie格式不正确，请确保包含 ltoken_v2 或 cookie_token_v2 等字段",
        }

    return await process_cookie(data.auth, cookie_str)


# ============ Auto Login (background task + polling) ============


class GeetestSolverModel(BaseModel):
    auth: str
    geetest_challenge: str
    geetest_validate: str
    geetest_seccode: str


async def _do_auto_login(auth: str, email: str, password: str):
    """Background task: runs genshin.py login with an async geetest solver."""
    entry = cache.get(auth)
    if not entry:
        return

    try:
        import genshin

        client = genshin.Client(region=genshin.Region.OVERSEAS)

        async def _solver(session_mmt):
            """Store geetest data in cache, then poll until frontend solves it."""
            current = cache.get(auth)
            if not current:
                raise TimeoutError("Session expired")

            current["geetest"] = {
                "gt": session_mmt.gt,
                "challenge": session_mmt.challenge,
                "session_id": session_mmt.session_id,
            }
            cache.set(auth, current)
            logger.info("[zzz登录] Geetest captcha triggered, waiting for user...")

            for _ in range(120):
                await asyncio.sleep(1)
                current = cache.get(auth)
                if not current:
                    raise TimeoutError("Session expired")
                solution = current.get("geetest_solution")
                if solution:
                    logger.info("[zzz登录] Geetest solution received from frontend")
                    return genshin.models.SessionMMTResult(
                        geetest_challenge=solution["geetest_challenge"],
                        geetest_validate=solution["geetest_validate"],
                        geetest_seccode=solution["geetest_seccode"],
                        session_id=session_mmt.session_id,
                    )

            raise TimeoutError("Geetest verification timed out")

        result = await client.os_login_with_password(
            email, password, encrypted=False, geetest_solver=_solver,
        )

        # Build cookie string from WebLoginResult
        cookie_parts = []
        for field in [
            "cookie_token_v2", "account_mid_v2", "account_id_v2",
            "ltoken_v2", "ltmid_v2", "ltuid_v2",
        ]:
            value = getattr(result, field, None)
            if value:
                cookie_parts.append(f"{field}={value}")

        if not cookie_parts:
            entry = cache.get(auth) or entry
            entry["status"] = "error"
            entry["result_msg"] = "登录成功但未获取到Cookie"
            cache.set(auth, entry)
            return

        cookie_str = "; ".join(cookie_parts)
        logger.info(
            f"[zzz登录] 自动登录成功, account_id: "
            f"{getattr(result, 'account_id_v2', '?')}"
        )
        await process_cookie(auth, cookie_str)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"[zzz登录] 自动登录失败: {error_msg}")

        entry = cache.get(auth) or entry
        if "AccountLoginFail" in error_msg or "password" in error_msg.lower():
            entry["result_msg"] = "邮箱或密码错误"
        elif "AccountDoesNotExist" in error_msg:
            entry["result_msg"] = "该邮箱未注册HoYoLab账号"
        else:
            entry["result_msg"] = f"登录失败: {error_msg}"
        entry["status"] = "error"
        cache.set(auth, entry)


@app.post("/zzz/login/auto")
async def zzz_login_auto(data: AutoLoginModel):
    """Start auto-login as a background task."""
    entry = cache.get(data.auth)
    if entry is None:
        return {"success": False, "msg": "会话已过期，请重新发送登录命令"}

    asyncio.create_task(_do_auto_login(data.auth, data.email, data.password))
    return {"success": True, "msg": "正在登录...", "pending": True}


@app.get("/zzz/login/status/{auth}")
async def zzz_login_status(auth: str):
    """Frontend polls this to check login progress / geetest requirement."""
    entry = cache.get(auth)
    if entry is None:
        return {"status": "expired"}

    if entry.get("status") == "success":
        return {"status": "success", "msg": entry.get("result_msg", "")}
    elif entry.get("status") == "error":
        return {"status": "error", "msg": entry.get("result_msg", "")}
    elif entry.get("geetest") and not entry.get("geetest_solution"):
        return {"status": "geetest", "geetest": entry["geetest"]}
    else:
        return {"status": "pending"}


@app.post("/zzz/login/geetest")
async def zzz_login_geetest(data: GeetestSolverModel):
    """Frontend submits the solved geetest captcha here."""
    entry = cache.get(data.auth)
    if entry is None:
        return {"success": False, "msg": "会话已过期"}

    entry["geetest_solution"] = {
        "geetest_challenge": data.geetest_challenge,
        "geetest_validate": data.geetest_validate,
        "geetest_seccode": data.geetest_seccode,
    }
    cache.set(data.auth, entry)
    logger.info("[zzz登录] Geetest solution stored, background task will pick it up")
    return {"success": True}
