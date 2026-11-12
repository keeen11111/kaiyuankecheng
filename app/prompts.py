"""提示词加载：所有提示词文本只维护在 docs/提示词.md，代码不硬编码任何一句提示词。"""

import re                                                       # 用正则切分文档中的提示词区块
from functools import lru_cache                                 # 提示词只读，进程内缓存一次

from app.config import resolve_path                            # 解析提示词文件的绝对路径
from app.response import BizError                              # 提示词缺失时抛业务异常

_BLOCK = re.compile(                                            # 匹配 <!-- BEGIN:KEY --> ... <!-- END:KEY -->
    r"<!--\s*BEGIN:([A-Z0-9_]+)\s*-->(.*?)<!--\s*END:\1\s*-->",  # 反向引用保证起止标签同名
    re.DOTALL,                                                  # 允许提示词跨多行
)


@lru_cache(maxsize=1)                                           # 一个进程只解析一次提示词文件
def load_prompts(file_path: str) -> dict[str, str]:
    """从 Markdown 文档解析出具名提示词模板。

    入参:
        file_path: config.yaml 中 prompts.file 指定的路径。
    出参:
        {"SYSTEM_RESEARCH": "模板文本", ...} 字典。
    失败时:
        文件不存在抛 FileNotFoundError；一个区块都没解析到抛 BizError("PROMPT_EMPTY")。
    """
    p = resolve_path(file_path)                                # 解析为绝对路径
    if not p.exists():                                         # 缺文件不能靠内置默认提示词蒙混
        raise FileNotFoundError(f"提示词文件不存在：{p}")     # 明确给出路径
    text = p.read_text(encoding="utf-8")                       # 显式 UTF-8，避免中文乱码
    found = {m.group(1): m.group(2).strip() for m in _BLOCK.finditer(text)}  # 提取全部区块
    if not found:                                              # 一个都没有说明文档格式被改坏了
        raise BizError("PROMPT_EMPTY", "提示词文件中没有合法的提示词区块")  # 快速失败
    return found                                               # 返回模板字典


def render(prompts: dict[str, str], key: str, **variables: str) -> str:
    """渲染指定提示词模板，替换 {{VAR}} 占位符。

    入参:
        prompts: load_prompts 返回的模板字典。
        key: 模板名。
        variables: 变量名到值的映射，值会被转成字符串。
    出参:
        渲染完成的提示词文本。
    失败时:
        模板名不存在抛 BizError("PROMPT_NOT_FOUND")；存在未替换占位符抛 BizError("PROMPT_VAR_MISSING")。
    """
    if key not in prompts:                                     # 模板名写错要早发现
        raise BizError("PROMPT_NOT_FOUND", f"缺少提示词模板：{key}")  # 明确到模板名
    text = prompts[key]                                        # 取原始模板
    for name, value in variables.items():                      # 逐个替换调用方提供的变量
        text = text.replace("{{" + name + "}}", str(value))   # 用简单替换，不引入模板引擎依赖
    left = re.findall(r"\{\{([A-Z0-9_]+)\}\}", text)          # 检查是否还有未填的占位符
    if left:                                                   # 未填变量会导致模型看到花括号而输出异常
        raise BizError("PROMPT_VAR_MISSING", f"提示词变量未赋值：{','.join(left)}")  # 明确列出缺失项
    return text                                                # 返回可直接发给模型的文本
