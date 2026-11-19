"""身份与权限：HMAC 令牌签发/校验、角色依赖、敏感字段脱敏。"""

import base64                                                   # Base64URL 编码令牌载荷
import hashlib                                                  # SHA-256 用于 PII 摘要与 HMAC 底层
import hmac                                                     # 常数时间比较与令牌签名
import json                                                     # 令牌载荷序列化
import time                                                     # 生成与校验过期时间
from typing import Callable                                     # 标注返回依赖函数的类型

from fastapi import Depends, Header                            # 依赖注入与请求头读取

from app.response import BizError                              # 鉴权失败统一抛业务异常


def _b64e(raw: bytes) -> str:
    """Base64URL 编码（去掉填充）。

    入参:
        raw: 原始字节。
    出参:
        无填充的 URL 安全字符串。
    失败时:
        输入非 bytes 时抛 TypeError。
    """
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")  # 去填充便于放进 Authorization 头


def _b64d(text: str) -> bytes:
    """Base64URL 解码（自动补填充）。

    入参:
        text: 无填充的 URL 安全字符串。
    出参:
        原始字节。
    失败时:
        字符串含非法字符时抛 binascii.Error，由 parse_token 转成鉴权失败。
    """
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))  # 补齐到 4 的倍数


def _sign(payload_b64: str, secret: str) -> str:
    """对令牌载荷做 HMAC-SHA256 签名。

    入参:
        payload_b64: Base64URL 编码后的载荷。
        secret: 来自环境变量的签名密钥。
    出参:
        64 位十六进制签名。
    失败时:
        secret 为空时抛 ValueError，启动时已被 validate_config 拦住。
    """
    return hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()  # 标准 HMAC


def issue_token(actor_id: str, pin: str, cfg: dict) -> dict:
    """校验账号口令并签发短期令牌。

    入参:
        actor_id: 账号 ID。
        pin: 明文口令，仅用于本次比较，不写日志。
        cfg: 全局配置，读取用户清单、密钥与有效期。
    出参:
        {"token", "expires_at", "actor": {"actor_id","name","roles"}}。
    失败时:
        账号不存在、口令错误抛 BizError("UNAUTHORIZED", 401)；不区分两种原因，防账号枚举。
    """
    user = next((u for u in cfg["security"]["users"] if u["actor_id"] == actor_id), None)  # 在配置清单里找账号
    if user is None or not hmac.compare_digest(str(pin), str(user["pin"])):  # 常数时间比较，防时序侧信道
        # 为什么卡在这里：账号枚举会让攻击者先摸清有效账号再撞口令，
        # 因此"账号不存在"与"口令错误"必须返回完全相同的错误码和文案。
        raise BizError("UNAUTHORIZED", "账号或口令错误", 401)  # 统一失败，不泄露账号是否存在
    exp = int(time.time()) + int(cfg["security"]["token_ttl_seconds"])  # 计算绝对过期时间戳
    payload = {"actor_id": user["actor_id"], "roles": user["roles"], "exp": exp}  # 载荷只放身份与过期时间
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))  # 紧凑序列化后编码
    token = f"{body}.{_sign(body, cfg['security']['token_secret'])}"  # 载荷与签名用点号拼接
    return {"token": token, "expires_at": exp,               # 返回令牌与过期时间
            "actor": {"actor_id": user["actor_id"], "name": user["name"], "roles": user["roles"]}}  # 返回身份摘要


def issue_demo_token(cfg: dict) -> dict:
    """签发"打开即看板"用的演示令牌。

    入参:
        cfg: 全局配置，读取 ui.auto_login_actor 与 security.users。
    出参:
        与 issue_token 完全一致的 {"token", "expires_at", "actor"}。
    失败时:
        ui.auto_login_actor 为空或账号不在清单内时抛 BizError("DEMO_LOGIN_DISABLED", 403)，
        不签发任何令牌，前端据此退回手输口令的登录页。
    """
    actor_id = str((cfg.get("ui") or {}).get("auto_login_actor") or "").strip()  # 取免登录账号，空串即关闭
    if not actor_id:                                           # 未配置账号时必须拒绝
        # 为什么卡在这里：这个入口等价于"跳过口令直接拿令牌"，一旦在生产被误开，
        # 任何人都能拿到管理员身份去复核和生效；因此必须由配置显式指定账号，为空即关闭。
        raise BizError("DEMO_LOGIN_DISABLED", "未开启免登录演示", 403)  # 统一拒绝，不下发令牌
    user = next((u for u in cfg["security"]["users"] if u["actor_id"] == actor_id), None)  # 账号必须在白名单内
    if user is None:                                           # 配置写错或账号被移除
        raise BizError("DEMO_LOGIN_DISABLED", "演示账号不存在", 403)  # 明确失败，不悄悄换成别的账号
    return issue_token(user["actor_id"], user["pin"], cfg)     # 复用同一签发逻辑，口令仍做常数时间比较


def parse_token(token: str, cfg: dict) -> dict:
    """校验令牌签名与有效期，还原身份。

    入参:
        token: "载荷.签名" 形式的令牌字符串。
        cfg: 全局配置，读取签名密钥。
    出参:
        {"actor_id", "roles", "exp"}。
    失败时:
        格式错误、签名不符、已过期、角色缺失均抛 BizError("UNAUTHORIZED", 401)。
    """
    if not token or "." not in token:                          # 基本格式校验，避免后续 split 越界
        raise BizError("UNAUTHORIZED", "缺少有效令牌", 401)   # 统一鉴权失败
    body, sig = token.rsplit(".", 1)                          # 从右侧分割，签名里不含点号
    expected = _sign(body, cfg["security"]["token_secret"])  # 用服务端密钥重算签名
    if not hmac.compare_digest(sig, expected):                # 常数时间比较，防伪造
        raise BizError("UNAUTHORIZED", "令牌签名无效", 401)   # 签名不符一律拒绝
    try:                                                        # 解析载荷可能失败
        payload = json.loads(_b64d(body).decode("utf-8"))     # 解码并反序列化
    except Exception:                                          # 任何解码异常都视为无效令牌
        raise BizError("UNAUTHORIZED", "令牌内容无效", 401)   # 不把解析细节返回给客户端
    if int(payload.get("exp", 0)) < int(time.time()):         # 过期判断，用服务端时钟
        raise BizError("UNAUTHORIZED", "令牌已过期", 401)     # 过期必须重新签发
    if not payload.get("actor_id") or not payload.get("roles"):  # 身份字段缺失的令牌不可信
        raise BizError("UNAUTHORIZED", "令牌身份不完整", 401) # 拒绝继续
    return payload                                             # 返回已验证身份


def current_actor(authorization: str = Header(default="")) -> dict:
    """FastAPI 依赖：从 Authorization 头解析当前身份。

    入参:
        authorization: 形如 "Bearer xxx" 的请求头，缺失时为空串。
    出参:
        已验证的身份字典。
    失败时:
        头缺失或格式不对抛 BizError("UNAUTHORIZED", 401)，请求不会进入业务函数。
    """
    if not authorization.startswith("Bearer "):               # 只接受 Bearer 方案
        raise BizError("UNAUTHORIZED", "请使用 Bearer 令牌", 401)  # 明确提示但不泄露细节
    from app.config import load_config                        # 延迟导入，避免模块加载顺序循环依赖
    return parse_token(authorization[len("Bearer "):].strip(), load_config())  # 去掉前缀后校验


def require_roles(*allowed: str) -> Callable:
    """生成角色校验依赖。

    入参:
        allowed: 允许访问的角色名列表，如 ("REVIEWER", "ADMIN")。
    出参:
        一个 FastAPI 依赖函数，校验通过时返回身份字典。
    失败时:
        依赖函数在无权限时抛 BizError("FORBIDDEN", 403)，业务代码完全不会执行。
    """

    def _dep(actor: dict = Depends(current_actor)) -> dict:
        """实际的角色校验依赖。

        入参:
            actor: 由 current_actor 解析出的身份字典。
        出参:
            校验通过时原样返回身份字典，供业务函数读取 actor_id。
        失败时:
            角色无交集时抛 BizError("FORBIDDEN", 403)。
        """
        # 为什么卡在这里：权限校验必须放在服务层依赖里，而不是靠前端隐藏按钮。
        # 前端可被绕过，直接调接口就能越权复核或生效；这里不通过，业务函数连数据库都不会碰。
        if not set(actor.get("roles", [])) & set(allowed):    # 求角色交集，空集即无权限
            raise BizError("FORBIDDEN", "当前角色无权执行此操作", 403)  # 统一拒绝
        return actor                                           # 返回身份，业务层可继续使用
    return _dep                                                # 返回依赖函数供路由使用


def mask_salary(cents: int, is_admin: bool, enabled: bool = True) -> dict:
    """按角色输出薪酬字段：非管理员只给掩码。

    入参:
        cents: 以分为单位的薪酬整数。
        is_admin: 当前身份是否具备 ADMIN 角色。
        enabled: 配置中的 data_redline.mask_salary_for_non_admin 开关。
    出参:
        {"base_salary_cents": 整数或 None, "base_salary_display": "****" 或 "¥20,000.00", "masked": bool}。
    失败时:
        cents 非整数时格式化会抛 TypeError，调用方应保证来自数据库 INTEGER 列。
    """
    if enabled and not is_admin:                               # 红线开启且不是管理员
        # 为什么卡在这里：接口层和查询接口都可能被复核人、经办人调用，
        # 只要有一次把分数值原样返回，薪酬就在系统外扩散且无法追回。
        return {"base_salary_cents": None, "base_salary_display": "****", "masked": True}  # 只给掩码
    return {"base_salary_cents": int(cents),                  # 管理员可见明文整数
            "base_salary_display": f"¥{int(cents) / 100:,.2f}",  # 转元并格式化，仅供展示
            "masked": False}                                   # 标记未脱敏


def hash_pii(value: str, cfg: dict) -> str:
    """把证件等敏感字段转成不可逆摘要。

    入参:
        value: 明文敏感值。
        cfg: 全局配置，读取 data_redline.pii_hash_algorithm。
    出参:
        十六进制摘要字符串。
    失败时:
        算法名非法时抛 ValueError；当前仅实现 sha256，避免静默使用弱哈希。
    """
    algo = cfg["data_redline"]["pii_hash_algorithm"]          # 从配置读算法名
    if algo != "sha256":                                      # 白名单校验，防止配置被改成 md5 之类的弱算法
        raise ValueError("仅支持 sha256 敏感字段摘要")       # 明确失败
    return hashlib.sha256(value.encode("utf-8")).hexdigest() # 返回不可逆摘要


def has_role(actor: dict, role: str) -> bool:
    """判断身份是否包含指定角色。

    入参:
        actor: 身份字典。
        role: 角色名。
    出参:
        True / False。
    失败时:
        actor 缺少 roles 字段时按空列表处理，返回 False。
    """
    return role in (actor.get("roles") or [])                 # 空列表兜底，避免 None 迭代报错
