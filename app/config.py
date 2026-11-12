"""配置加载模块：只从 config.yaml 与环境变量取值，代码里不出现任何密钥或模型地址字面量。"""

import os                                                       # 读取环境变量，用于注入密钥与运行期覆盖
import re                                                       # 识别 ${ENV_VAR} 占位符
from functools import lru_cache                                 # 配置只读且进程内复用，避免每个请求重复读盘
from pathlib import Path                                        # 解析项目根路径，保证任意工作目录下都能找到配置
from typing import Any                                          # 类型标注用，标注递归解析函数的返回值

import yaml                                                     # 解析 YAML 配置文件

PROJECT_ROOT = Path(__file__).resolve().parent.parent           # 项目根目录：app/ 的上一级
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"              # 默认配置文件路径，可被 APP_CONFIG 环境变量覆盖
_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")  # 只匹配整串 ${VAR}，避免误伤普通文本


def _resolve_env(node: Any, missing: list[str]) -> Any:
    """递归把配置中的 ${VAR} 占位符替换成环境变量值。

    入参:
        node: 配置树中的任意节点，可以是字典、列表或字符串。
        missing: 出参列表，记录引用了但环境中不存在的变量名。
    出参:
        替换后的同结构配置树；普通字符串原样返回。
    失败时:
        环境变量缺失时该位置置为 None 并记入 missing，不抛异常；由 validate_config 统一判定是否致命。
    """
    if isinstance(node, dict):                                  # 字典需要逐键递归，保证嵌套配置也能解析
        return {k: _resolve_env(v, missing) for k, v in node.items()}  # 用字典推导式保持原结构
    if isinstance(node, list):                                  # 列表需要逐元素递归，用户清单就是列表
        return [_resolve_env(v, missing) for v in node]         # 用列表推导式保持元素顺序
    if isinstance(node, str):                                   # 只有字符串才可能是占位符
        m = _ENV_PATTERN.match(node.strip())                    # 尝试匹配 ${VAR} 形式
        if m:                                                   # 命中占位符，说明这是敏感值引用
            name = m.group(1)                                  # 取出环境变量名
            value = os.environ.get(name)                       # 从进程环境读取真实值
            if value is None or value == "":                    # 环境未设置或为空串都视为缺失
                missing.append(name)                           # 记录缺失项，稍后统一报错
                return None                                    # 返回 None，让校验阶段明确失败
            return value                                       # 返回注入后的真实值
    return node                                                # 非字符串或非占位符原样返回


def _apply_overrides(cfg: dict) -> dict:
    """用少量运行期环境变量覆盖配置，仅用于临时数据库与 mock 模型自测。

    入参:
        cfg: 已完成环境变量解析的配置字典。
    出参:
        应用覆盖后的配置字典（原地修改并返回同一对象）。
    失败时:
        环境变量值类型错误时不抛异常，按字符串写入；由后续使用方校验。
    """
    mapping = {                                                # 覆盖表：环境变量名 -> 配置中的点分路径
        "APP_DB_PATH": ("db", "path"),                         # 验证脚本用临时库，避免污染正式数据
        "APP_LLM_MODE": ("llm", "mode"),                      # mock / http 切换，验证脚本无需真实模型
        "APP_LLM_BASE_URL": ("llm", "base_url"),              # 本地千问地址可由部署环境注入
        "APP_LLM_MODEL": ("llm", "model"),                    # 模型名可由部署环境注入
        "APP_LLM_API_KEY": ("llm", "api_key"),                # 本地网关密钥可由部署环境注入
        "APP_PROMPTS_FILE": ("prompts", "file"),              # 提示词文件路径可由部署环境注入
    }                                                          # 覆盖表结束
    for env_name, path in mapping.items():                     # 逐项检查是否存在覆盖
        if env_name in os.environ and os.environ[env_name]:    # 只处理非空值，空串不覆盖默认配置
            cur = cfg                                          # 从根节点开始下钻
            for key in path[:-1]:                              # 逐层进入中间字典
                cur = cur.setdefault(key, {})                   # 中间层缺失时补空字典，保证不 KeyError
            cur[path[-1]] = os.environ[env_name]               # 写入叶子节点的覆盖值
    return cfg                                                 # 返回同一配置对象，方便链式调用


def validate_config(cfg: dict, missing: list[str]) -> None:
    """校验配置的必填项与取值合法性，启动阶段快速失败。

    入参:
        cfg: 已解析并覆盖后的配置字典。
        missing: _resolve_env 收集到的缺失环境变量名列表。
    出参:
        无返回值，校验通过静默返回。
    失败时:
        抛出 ValueError 说明具体缺失项或非法取值，进程不予启动，避免出现"带病运行"的服务。
    """
    secret = cfg.get("security", {}).get("token_secret")        # 令牌签名密钥是最高优先级的必填项
    if not secret:                                              # 缺失或为空一律拒绝启动
        raise ValueError(f"缺少令牌签名密钥，请设置环境变量；缺失项={missing}")  # 明确告知运维如何修复
    if cfg.get("llm", {}).get("mode") not in ("mock", "http"):  # 只允许两种模式，拼错要早发现
        raise ValueError("llm.mode 只能是 mock 或 http")        # 非法模式直接报错
    if cfg.get("llm", {}).get("mode") == "http":                # 只有真实调用模式才强制校验地址
        if not cfg["llm"].get("base_url"):                     # 地址为空无法发起调用
            raise ValueError("llm.mode=http 时必须配置 llm.base_url")  # 提示补全配置
    if not cfg.get("security", {}).get("users"):               # 没有任何身份则所有接口都无法鉴权
        raise ValueError("security.users 不能为空")            # 拒绝无身份清单的启动
    for user in cfg["security"]["users"]:                      # 逐个用户检查口令是否已注入
        pin_env = user.get("pin_env")                          # 取该用户口令对应的环境变量名
        user["pin"] = os.environ.get(pin_env, "") if pin_env else ""  # 把真实口令挂到运行时对象上
        if not user["pin"]:                                    # 口令缺失则该账号无法签发令牌
            raise ValueError(f"用户 {user.get('actor_id')} 的口令未注入，请设置环境变量 {pin_env}")  # 明确到账号
    if not cfg.get("prompts", {}).get("file"):                 # 提示词文件路径是 harness 的必需输入
        raise ValueError("prompts.file 不能为空")              # 拒绝无提示词的启动


@lru_cache(maxsize=1)                                           # 进程内单例缓存，配置只读一次
def load_config() -> dict:
    """加载并校验全局配置，是整个进程唯一的配置入口。

    入参:
        无显式入参；路径来自 APP_CONFIG 环境变量或项目根目录下 config.yaml。
    出参:
        完整配置字典，包含 app/server/db/security/llm/harness/kb/prompts/data_redline 各段。
    失败时:
        配置文件不存在、YAML 语法错误、必填项缺失时抛 ValueError/FileNotFoundError，服务拒绝启动。
    """
    path = Path(os.environ.get("APP_CONFIG", DEFAULT_CONFIG_PATH))  # 允许部署方指定其他配置路径
    if not path.exists():                                       # 配置缺失时不能靠默认值糊弄过去
        raise FileNotFoundError(f"配置文件不存在：{path}")      # 明确给出绝对路径
    with path.open("r", encoding="utf-8") as f:                # 显式 UTF-8，避免中文配置在 Windows 上乱码
        raw = yaml.safe_load(f)                                # safe_load 禁止任意对象构造，防止配置投毒
    if not isinstance(raw, dict):                              # 顶层必须是映射结构
        raise ValueError("config.yaml 顶层必须是字典")         # 结构错误直接失败
    missing: list[str] = []                                    # 收集缺失的环境变量名
    cfg = _resolve_env(raw, missing)                           # 递归解析 ${VAR} 占位符
    cfg = _apply_overrides(cfg)                                # 应用运行期覆盖（临时库、mock 模式等）
    validate_config(cfg, missing)                              # 统一校验必填项，失败即终止启动
    return cfg                                                 # 返回可用配置


def resolve_path(rel_or_abs: str) -> Path:
    """把配置中的相对路径解析成基于项目根目录的绝对路径。

    入参:
        rel_or_abs: 配置里写的相对路径或绝对路径字符串。
    出参:
        解析后的 Path 对象。
    失败时:
        传入非字符串时抛 TypeError；不检查文件是否存在，由调用方决定。
    """
    p = Path(rel_or_abs)                                       # 先转成 Path，兼容两种写法
    return p if p.is_absolute() else PROJECT_ROOT / p          # 绝对路径直接用，相对路径挂到项目根
