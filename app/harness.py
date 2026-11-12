"""现场调研 harness：自研多轮编排循环，模型只负责选工具，落库与生效由人决定。"""

import json                                                    # 序列化会话 transcript
from typing import Any                                          # 标注工具调用与结论结构

from app import kb                                             # 知识库检索工具实现
from app.db import dumps, new_id, now_utc                      # JSON 序列化、ID 与时间戳
from app.llm import QwenClient, extract_json                   # 本地千问客户端与结构化解析
from app.prompts import load_prompts, render                  # 从 docs 加载并渲染提示词
from app.response import BizError                              # 工具越权与输出异常时抛业务异常

ALLOWED_TOOLS = {"kb_search", "ask_field", "finish"}          # 工具白名单：模型只能在这三个里选
_ALLOWED_RISK = {"LOW", "MEDIUM", "HIGH"}                     # 结论风险等级白名单
_ALLOWED_RECOMMEND = {"SUBMIT_REVIEW", "HOLD"}                # 结论建议白名单，模型无权给"直接生效"


def _clip(value: Any, limit: int = 500) -> str:
    """把任意值转成限长字符串，防止模型输出撑爆数据库或注入下游。

    入参:
        value: 任意值。
        limit: 最大字符数，默认 500。
    出参:
        限长字符串。
    失败时:
        value 不可转成字符串时抛 TypeError，由 _validate_conclusion 转成 BAD_CONCLUSION。
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)  # 非字符串序列化成 JSON
    return text[:limit]                                        # 硬截断，避免长文本攻击


def _validate_conclusion(raw: Any) -> dict:
    """安全审查：校验模型给出的结论结构，缺字段或越权值一律拒绝。

    入参:
        raw: 模型 finish 工具里的 conclusion 对象。
    出参:
        规范化后的结论字典，含 summary/findings/evidence/risk_level/recommend。
    失败时:
        结构不符、类型不符、枚举越权时抛 BizError("RESEARCH_BAD_CONCLUSION")，不落库。
    """
    # 为什么卡在这里：模型输出是不可信输入。若允许它自由填 risk_level 或 recommend，
    # 它完全可能给出"直接生效"之类的建议，下游一旦误用就绕过双人复核；这里只放行两个枚举值。
    if not isinstance(raw, dict):                             # 结论必须是对象
        raise BizError("RESEARCH_BAD_CONCLUSION", "结论必须是对象")  # 拒绝
    required = ("summary", "findings", "evidence", "risk_level", "recommend")  # 必需字段
    if any(k not in raw for k in required):                  # 任一缺失都视为不完整取证
        raise BizError("RESEARCH_BAD_CONCLUSION", "结论缺少必需字段")  # 拒绝
    if not isinstance(raw["findings"], list) or not isinstance(raw["evidence"], list):  # 两个列表类型
        raise BizError("RESEARCH_BAD_CONCLUSION", "findings 与 evidence 必须是列表")  # 拒绝
    risk = str(raw["risk_level"]).upper()                    # 归一化风险等级
    recommend = str(raw["recommend"]).upper()               # 归一化建议
    if risk not in _ALLOWED_RISK:                           # 枚举白名单校验
        raise BizError("RESEARCH_BAD_CONCLUSION", "结论风险等级不合法")  # 拒绝
    if recommend not in _ALLOWED_RECOMMEND:                 # 模型无权建议直接生效
        raise BizError("RESEARCH_BAD_CONCLUSION", "结论建议值不合法")  # 拒绝
    return {"summary": _clip(raw["summary"]),              # 摘要限长
            "findings": [_clip(x, 300) for x in raw["findings"][:20]],  # 发现项逐条限长并限量
            "evidence": [_clip(x, 300) for x in raw["evidence"][:20]],  # 证据逐条限长并限量
            "risk_level": risk,                            # 已校验的枚举
            "recommend": recommend}                        # 已校验的枚举


def _pick_field_answer(question: str, field_answers: dict[str, str]) -> str:
    """从现场答复集合里匹配模型提的问题。

    入参:
        question: 模型本次提问文本。
        field_answers: 请求里传入的"问题 -> 答复"映射。
    出参:
        匹配到的答复；匹配不到时返回明确的缺口标记。
    失败时:
        field_answers 含非字符串值时按字符串转换处理，不抛异常。
    """
    if question in field_answers:                           # 优先精确匹配
        return _clip(field_answers[question], 500)          # 返回限长答复
    for key, value in field_answers.items():                # 退而求其次做包含匹配
        if key and (key in question or question in key):   # 任一方向包含即认为问的是同一件事
            return _clip(value, 500)                       # 返回对应答复
    return "现场未提供书面答复（存在取证缺口）"            # 明确标注缺口，不伪造答复


def _execute_tool(conn, cfg: dict, tool: str, args: dict,
                  field_answers: dict[str, str]) -> dict:
    """执行模型选定的白名单工具。

    入参:
        conn: 当前事务中的数据库连接。
        cfg: 全局配置，读取 harness.kb_top_k。
        tool: 工具名。
        args: 工具参数。
        field_answers: 现场答复集合。
    出参:
        {"tool": 工具名, "result": 工具结果}。
    失败时:
        工具不在白名单抛 BizError("HARNESS_TOOL_FORBIDDEN")；参数非法抛
        BizError("HARNESS_BAD_ARGS")；知识库检索失败由 kb 层抛出。
    """
    # 为什么卡在这里：模型可以选择工具，但绝不能执行任意代码或任意 SQL。
    # 只放行三个固定函数，并对参数做类型与长度限制，等于把提示词注入的破坏面收窄到只读检索。
    if tool not in ALLOWED_TOOLS:                          # 白名单之外的工具名
        raise BizError("HARNESS_TOOL_FORBIDDEN", f"模型请求了未授权工具：{tool}")  # 硬拒绝
    if not isinstance(args, dict):                         # 参数必须是对象
        raise BizError("HARNESS_BAD_ARGS", "工具参数必须是对象")  # 拒绝
    if tool == "kb_search":                               # 只读检索知识库
        query = _clip(args.get("query", ""), 200)          # 检索词限长
        result = {"hits": kb.search(conn, query, int(cfg["harness"]["kb_top_k"]))}  # 执行检索
    elif tool == "ask_field":                             # 读取现场人员答复
        question = _clip(args.get("question", ""), 300)   # 问题限长
        result = {"question": question, "answer": _pick_field_answer(question, field_answers)}  # 取答复
    else:                                                  # 只剩 finish
        result = {"finished": True}                        # 结束标记，结论由调用方再校验
    return {"tool": tool, "result": result}               # 返回统一工具执行结果


def run_research(conn, cfg: dict, version: dict, question: str,
                 field_answers: dict[str, str], actor_id: str) -> dict:
    """跑一场现场调研，产出待人工确认的结构化结论。

    入参:
        conn: 当前写事务中的数据库连接。
        cfg: 全局配置。
        version: 变更版本字典（已脱敏上下文由调用方构造）。
        question: 本场调研核心问题。
        field_answers: 现场人员答复映射。
        actor_id: 发起人 ID，写入会话与审计上下文。
    出参:
        {"session_id","status","rounds","conclusion","transcript"}，status 恒为 WAIT_CONFIRM。
    失败时:
        工具越权、结论不合规、超过最大轮数、模型不可用时抛对应 BizError；
        事务由调用方回滚，版本不会停留在 RESEARCHING，也不会自动进入复核。
    """
    prompts = load_prompts(cfg["prompts"]["file"])         # 从 docs 读取提示词，代码不含提示词文本
    client = QwenClient(cfg)                               # 构造本地千问客户端（含内网地址审查）
    max_rounds = int(cfg["harness"]["max_rounds"])        # 最大轮数，防死循环
    system = render(prompts, "SYSTEM_RESEARCH",           # 渲染系统提示词
                    MAX_ROUNDS=str(max_rounds),           # 注入轮数上限
                    ALLOWED_TOOLS=", ".join(sorted(ALLOWED_TOOLS)),  # 注入工具白名单
                    OUTPUT_SCHEMA=dumps({                 # 注入工具调用的输出格式说明
                        "tool": "kb_search | ask_field | finish",  # 工具名取值
                        "args": "对应工具的参数对象",     # 参数说明
                    }))                                    # 渲染结束
    version_context = dumps({                             # 只把脱敏后的版本上下文交给模型
        "version_id": version["version_id"],             # 版本 ID
        "emp_id": version["emp_id"],                     # 工号
        "reason": _clip(version["reason"], 300),        # 变更事由限长
        "grade": version.get("grade", ""),              # 职级（脱敏上下文）
        "salary": "****",                               # 薪酬恒为掩码，模型永远拿不到明文金额
    })                                                     # 上下文构造结束
    transcript: list[dict[str, Any]] = []                # 累积多轮工具调用记录
    conclusion: dict | None = None                       # 最终结论，未产出前为 None
    rounds = 0                                            # 实际执行轮数
    for round_no in range(1, max_rounds + 1):            # 逐轮驱动，上限由配置锁死
        rounds = round_no                                # 记录当前轮次
        user = render(prompts, "USER_ROUND",             # 渲染本轮用户提示词
                      ROUND=str(round_no),               # 当前轮次
                      MAX_ROUNDS=str(max_rounds),        # 总轮次
                      QUESTION=_clip(question, 500),     # 调研问题
                      VERSION_CONTEXT=version_context,   # 脱敏版本上下文
                      TRANSCRIPT=dumps(transcript[-6:]),  # 只带最近 6 轮，控制上下文长度
                      FIELD_ANSWERS=dumps({_clip(k, 200): _clip(v, 300)  # 现场答复限长
                                           for k, v in list(field_answers.items())[:20]}))  # 最多 20 条
        messages = [{"role": "system", "content": system},  # 系统提示词
                    {"role": "user", "content": user}]   # 本轮用户提示词
        raw = client.chat(messages)                       # 调用本地千问（外发前会做安全审查）
        call = extract_json(raw)                         # 解析模型输出，非 JSON 直接失败
        tool = str(call.get("tool", ""))                 # 取工具名
        args = call.get("args") or {}                    # 取参数，缺省为空对象
        outcome = _execute_tool(conn, cfg, tool, args, field_answers)  # 白名单执行工具
        if tool == "finish":                             # 模型宣布结束，校验结论结构
            conclusion = _validate_conclusion(args.get("conclusion"))  # 不合规直接抛错回滚
        transcript.append({"round": round_no,           # 记录轮次
                           "model_call": call,          # 记录模型原始工具调用，便于追溯
                           "tool_result": outcome})     # 记录工具结果
        if conclusion is not None:                      # 已拿到合规结论
            break                                        # 结束循环
    if conclusion is None:                              # 跑满轮数仍未给出结论
        raise BizError("RESEARCH_MAX_ROUNDS", "调研超过最大轮数且未产出结论")  # 明确失败
    session_id = new_id("ses")                          # 生成会话 ID
    conn.execute(                                       # 写入调研会话，状态固定为待人工确认
        "INSERT INTO research_sessions(session_id,version_id,status,question,transcript_json,conclusion_json,confirmed_by,created_at,confirmed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",                    # 占位符防注入
        (session_id, version["version_id"], "WAIT_CONFIRM", question,  # 待确认状态
         dumps(transcript), dumps(conclusion), None, now_utc(), None),  # 结论已结构化但尚未生效
    )                                                   # 插入结束
    # 为什么卡在这里：模型结论只能落到 sessions 表，绝不写 salary_records、绝不改版本状态。
    # 生成式输出可能出错，若自动进入生效链路，双人复核就形同虚设，责任也无法归属到人。
    return {"session_id": session_id, "status": "WAIT_CONFIRM",  # 返回待确认会话
            "rounds": rounds, "conclusion": conclusion, "transcript": transcript,
            "actor_id": actor_id}                       # 附带发起人与完整轨迹
