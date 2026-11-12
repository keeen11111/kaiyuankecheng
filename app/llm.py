"""本地千问适配层：只走 OpenAI 兼容 /v1/chat/completions，并强制阻止数据外发。"""

import ipaddress                                               # 判断 IP 是否为回环/私网地址
import json                                                    # 解析模型返回的结构化 JSON
import re                                                      # 剥离模型输出里的 Markdown 代码块围栏
from typing import Any                                          # 标注消息与返回结构
from urllib.parse import urlparse                              # 解析模型地址中的主机名

import httpx                                                   # 调用本地推理网关

from app.response import BizError                              # 调用失败统一抛业务异常


def _assert_private_host(base_url: str, allowed_hosts: list[str], forbid_public: bool) -> str:
    """安全审查：模型地址必须指向本机或内网。

    入参:
        base_url: 配置中的模型服务地址。
        allowed_hosts: 允许的内网主机名/前缀列表。
        forbid_public: 配置中的 data_redline.forbid_public_llm_host 开关。
    出参:
        校验通过的 base_url（去尾部斜杠）。
    失败时:
        地址为空、无主机名、公网地址时抛 BizError("LLM_HOST_FORBIDDEN")，绝不发起请求。
    """
    parsed = urlparse(base_url)                                # 解析出 scheme / host / path
    host = parsed.hostname or ""                              # 取主机名，可能为空
    if parsed.scheme not in ("http", "https") or not host:   # 只接受 HTTP(S) 且必须有主机
        raise BizError("LLM_HOST_FORBIDDEN", "模型地址不合法")  # 明确拒绝
    in_whitelist = any(host == h.rstrip(".") or host.startswith(h)  # 精确匹配或前缀匹配
                       for h in allowed_hosts)                # 遍历配置中的白名单
    try:                                                        # 尝试把主机当 IP 解析
        ip = ipaddress.ip_address(host)                        # 非 IP 会抛 ValueError
        is_private = ip.is_loopback or ip.is_private or ip.is_link_local  # 回环/私网/链路本地都算内网
    except ValueError:                                         # 主机名不是 IP（如 localhost）
        is_private = False                                     # 交给白名单判断
    if forbid_public and not (in_whitelist or is_private):    # 红线开启且既不在白名单也非私网
        # 为什么卡在这里：薪酬与现场调研材料属于高敏数据，一旦有人把 base_url
        # 改成公网大模型地址，接口调用就会把数据发到内网之外，且事后无法召回。
        raise BizError("LLM_HOST_FORBIDDEN", "模型地址必须是本机或内网，禁止公网端点")  # 硬拒绝
    return base_url.rstrip("/")                                # 返回规范化地址


class QwenClient:
    """本地千问客户端：mock 模式用于离线自测，http 模式调用本地推理服务。"""  # 类说明

    def __init__(self, cfg: dict) -> None:
        """初始化客户端并做地址安全审查。

        入参:
            cfg: 全局配置字典，读取 llm 与 data_redline 段。
        出参:
            无返回值，初始化实例属性。
        失败时:
            http 模式下地址非法抛 BizError("LLM_HOST_FORBIDDEN")，进程启动阶段即可发现。
        """
        self.cfg = cfg                                         # 保存配置引用，不在代码里写死任何地址
        self.mode = cfg["llm"]["mode"]                        # mock 或 http
        self.model = cfg["llm"]["model"]                      # 模型名
        self.timeout = float(cfg["llm"]["timeout_seconds"])  # 单次超时秒数
        self.retries = int(cfg["llm"]["max_retries"])        # 最大重试次数
        self.max_tokens = int(cfg["llm"]["max_tokens"])      # 最大生成 token
        self.temperature = float(cfg["llm"]["temperature"])  # 温度
        self.api_key = cfg["llm"].get("api_key") or ""       # 本地网关密钥，可为空
        self.base_url = ""                                    # http 模式下才赋值
        if self.mode == "http":                               # 只有真实调用才需要校验地址
            self.base_url = _assert_private_host(             # 内网白名单校验
                cfg["llm"]["base_url"],                      # 配置中的地址
                list(cfg["llm"]["allowed_hosts"]),           # 配置中的白名单
                bool(cfg["data_redline"]["forbid_public_llm_host"]),  # 红线开关
            )                                                  # 校验结束

    def _assert_clean_messages(self, messages: list[dict]) -> None:
        """安全审查：发给模型的消息只能含 role/content，不得夹带凭据。

        入参:
            messages: OpenAI 兼容的消息列表。
        出参:
            无返回值，校验通过静默返回。
        失败时:
            消息结构异常或出现敏感键时抛 BizError("LLM_INPUT_FORBIDDEN")。
        """
        # 为什么卡在这里：提示词由多个模块拼装，一旦有人误把整个配置对象或令牌塞进上下文，
        # 模型网关日志就会留下密钥；这里用白名单结构把外发内容锁死。
        for m in messages:                                     # 逐条检查
            if not isinstance(m, dict) or set(m.keys()) - {"role", "content"}:  # 只允许两个键
                raise BizError("LLM_INPUT_FORBIDDEN", "模型输入含有非法字段")  # 拒绝发送
            if m.get("role") not in ("system", "user", "assistant"):  # 角色白名单
                raise BizError("LLM_INPUT_FORBIDDEN", "模型输入角色不合法")  # 拒绝发送
            if not isinstance(m.get("content"), str):        # content 必须是已渲染好的字符串
                raise BizError("LLM_INPUT_FORBIDDEN", "模型输入内容格式不合法")  # 拒绝发送
        blob = json.dumps(messages, ensure_ascii=False).lower()  # 拼成文本做关键词兜底
        for word in ("token_secret", "api_key", "pin\":", "bearer "):  # 常见凭据特征
            if word in blob:                                   # 命中即认为上下文被污染
                raise BizError("LLM_INPUT_FORBIDDEN", "模型输入疑似包含凭据")  # 硬拒绝

    def _mock_reply(self, messages: list[dict]) -> str:
        """离线自测用的确定性回复，按当前轮次模拟工具调用。

        入参:
            messages: 本轮完整消息列表，最后一条的 user 内容含 "当前轮次: N"。
        出参:
            JSON 字符串，形如 {"tool":"...","args":{...}}。
        失败时:
            解析不到轮次时按第 1 轮处理，不抛异常，保证验证脚本稳定。
        """
        last = messages[-1]["content"] if messages else ""   # 取最后一轮用户内容
        m = re.search(r"当前轮次[:：]\s*(\d+)", last)         # 提取轮次编号
        round_no = int(m.group(1)) if m else 1                # 默认按第一轮
        if round_no <= 1:                                      # 第一轮：先查制度知识库
            reply = {"tool": "kb_search",                     # 工具名
                     "args": {"query": "薪酬调整审批规范 现场调研"}}  # 检索参数
        elif round_no == 2:                                   # 第二轮：向现场取证
            reply = {"tool": "ask_field",                     # 工具名
                     "args": {"question": "请确认调薪生效月份、绩效书面依据及员工是否知悉"}}  # 问题
        else:                                                  # 第三轮及以后：输出结构化结论
            reply = {"tool": "finish",                        # 结束工具
                     "args": {"conclusion": {                 # 结论体
                         "summary": "已完成现场取证，制度依据齐备，建议提交双人复核。",  # 摘要
                         "findings": ["岗位职级未变更", "绩效结论有书面依据", "生效月份已确认"],  # 发现项
                         "evidence": ["薪酬调整审批规范", "现场调研取证要点", "现场人员答复"],  # 证据
                         "risk_level": "LOW",                 # 风险等级
                         "recommend": "SUBMIT_REVIEW",        # 建议动作
                     }}}                                       # 结论结束
        return json.dumps(reply, ensure_ascii=False)          # 序列化，中文不转义

    def _http_reply(self, messages: list[dict]) -> str:
        """调用本地千问的 OpenAI 兼容接口。

        入参:
            messages: 已渲染、已脱敏的消息列表。
        出参:
            模型首条回复的文本内容。
        失败时:
            网络异常按配置重试；仍失败或返回体异常抛 BizError("LLM_UNAVAILABLE"/"LLM_HTTP_ERROR")，
            绝不返回空串让上层误判为成功。
        """
        url = self.base_url + "/chat/completions"             # 拼接标准兼容端点
        headers = {"Content-Type": "application/json"}       # 固定 JSON 请求
        if self.api_key:                                      # 本地网关需要鉴权时加头
            headers["Authorization"] = f"Bearer {self.api_key}"  # 密钥来自环境变量，不写日志
        payload = {"model": self.model, "messages": messages,  # 组装请求体
                   "temperature": self.temperature, "max_tokens": self.max_tokens}  # 参数来自配置
        last_err = ""                                          # 记录最后一次错误类型名
        for attempt in range(self.retries + 1):               # 含首次调用共 retries+1 次
            try:                                               # 保护网络调用
                resp = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)  # 同步调用
                if resp.status_code >= 400:                   # 网关返回错误状态
                    raise BizError("LLM_HTTP_ERROR", f"本地模型返回状态码 {resp.status_code}")  # 不含响应体
                data = resp.json()                            # 解析 JSON 响应
                return data["choices"][0]["message"]["content"]  # 取首条回复文本
            except BizError:                                   # 业务错误不重试，直接上抛
                raise                                          # 保持错误码
            except (httpx.RequestError, KeyError, IndexError, ValueError) as e:  # 网络/结构异常可重试
                last_err = type(e).__name__                   # 只记类型，不记可能含敏感内容的文本
                if attempt == self.retries:                   # 已用尽重试次数
                    break                                      # 退出循环统一报错
        raise BizError("LLM_UNAVAILABLE", f"本地千问调用失败（{last_err}）")  # 明确失败，不让流程继续

    def chat(self, messages: list[dict]) -> str:
        """统一的模型对话入口。

        入参:
            messages: OpenAI 兼容消息列表。
        出参:
            模型回复文本。
        失败时:
            输入审查不通过抛 BizError("LLM_INPUT_FORBIDDEN")；调用失败由子方法抛出对应错误码。
        """
        self._assert_clean_messages(messages)                 # 外发前最后一道安全审查
        if self.mode == "mock":                              # 离线自测分支
            return self._mock_reply(messages)                # 返回确定性脚本化回复
        return self._http_reply(messages)                    # 真实调用本地千问


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出中解析出唯一的结构化 JSON 对象。

    入参:
        text: 模型原始回复，可能带 ```json 围栏或前后解释文字。
    出参:
        解析后的字典。
    失败时:
        无 JSON、JSON 非法、顶层不是对象时抛 BizError("LLM_BAD_JSON")，绝不把自由文本当结论落库。
    """
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()  # 去掉代码围栏
    start, end = cleaned.find("{"), cleaned.rfind("}")      # 取最外层花括号边界
    if start < 0 or end <= start:                           # 没找到合法对象
        raise BizError("LLM_BAD_JSON", "模型输出中未找到结构化结论")  # 统一错误码
    try:                                                      # 保护反序列化
        obj = json.loads(cleaned[start:end + 1])            # 只解析对象片段
    except json.JSONDecodeError as e:                        # 模型输出不合规
        raise BizError("LLM_BAD_JSON", f"模型输出不是合法 JSON：{e.msg}")  # 不含原文，防泄露
    if not isinstance(obj, dict):                           # 顶层必须是对象
        raise BizError("LLM_BAD_JSON", "模型输出顶层必须是对象")  # 拒绝数组/标量
    return obj                                              # 返回结构化结果
