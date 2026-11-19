"""演示数据脚本：给前端页面造出覆盖全部状态的版本，便于直接查看与试操作。"""

import os                                                       # 从环境变量读取演示口令与服务地址
import sys                                                      # 失败时以非零码退出

import httpx                                                    # 调用运行中的 FastAPI 服务

BASE = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8000")  # 服务地址可由环境变量覆盖
PINS = {                                                        # 演示账号口令，全部来自环境变量，不硬编码
    "u_operator": os.environ.get("APP_PIN_OPERATOR", ""),      # 经办人
    "u_reviewer_a": os.environ.get("APP_PIN_REVIEWER_A", ""),  # 第一复核人
    "u_reviewer_b": os.environ.get("APP_PIN_REVIEWER_B", ""),  # 第二复核人
    "u_admin": os.environ.get("APP_PIN_ADMIN", ""),            # 管理员
}                                                              # 口令表结束
FIELD_ANSWER = {                                                # 现场书面答复，键与 mock 模型提问一致
    "请确认调薪生效月份、绩效书面依据及员工是否知悉": "生效月份为次月，绩效结论有书面评级表，员工已签字知悉。"  # 答复内容
}                                                              # 答复表结束


def token(client: httpx.Client, actor_id: str) -> dict:
    """签发指定账号的令牌并组装请求头。

    入参:
        client: httpx 客户端。
        actor_id: 账号 ID。
    出参:
        含 Authorization 的请求头字典。
    失败时:
        口令未注入或签发失败时抛 RuntimeError，脚本立即终止。
    """
    pin = PINS.get(actor_id, "")                               # 从环境变量表取口令
    if not pin:                                                # 未注入则不能继续
        raise RuntimeError(f"缺少 {actor_id} 的演示口令环境变量")  # 明确提示
    r = client.post("/api/v1/auth/token", json={"actor_id": actor_id, "pin": pin})  # 调签发接口
    if r.status_code != 200 or not r.json().get("ok"):         # 签发失败
        raise RuntimeError(f"登录失败 {actor_id}: {r.text}")   # 抛出原始响应便于定位
    return {"Authorization": "Bearer " + r.json()["data"]["token"]}  # 返回 Bearer 头


def call(client: httpx.Client, method: str, path: str, headers: dict, body: dict | None = None) -> dict:
    """统一调用接口并校验统一响应结构。

    入参:
        client: httpx 客户端。
        method: HTTP 方法。
        path: 接口路径。
        headers: 请求头。
        body: 可选 JSON 请求体。
    出参:
        响应体中的 data 部分。
    失败时:
        HTTP 非 2xx 或 ok=false 时抛 RuntimeError，带上错误码与提示。
    """
    r = client.request(method, path, headers=headers, json=body)  # 发起请求
    j = r.json()                                               # 解析统一响应
    if not j.get("ok"):                                        # 业务失败
        raise RuntimeError(f"{path} -> {j.get('error', {}).get('code')}: {j.get('error', {}).get('msg')}")  # 抛错
    return j.get("data")                                      # 返回数据


def create(client: httpx.Client, hdr: dict, cents: int, reason: str) -> str:
    """创建一个草稿版本。

    入参:
        client: httpx 客户端。
        hdr: 经办人请求头。
        cents: 变更后月薪（分）。
        reason: 变更事由。
    出参:
        版本 ID。
    失败时:
        创建失败由 call 抛 RuntimeError。
    """
    d = call(client, "POST", "/api/v1/changes", hdr, {       # 调创建接口
        "emp_id": "E001", "new_base_salary_cents": cents,     # 工号与金额
        "reason": reason,                                    # 事由
        "research_question": "核实岗位职级、绩效依据与生效月份",  # 调研问题
    })                                                        # 调用结束
    return d["version_id"]                                    # 返回版本 ID


def prepare(client: httpx.Client, op: dict, vid: str, submit: bool = True) -> None:
    """跑调研并人工确认，可选提交复核。

    入参:
        client: httpx 客户端。
        op: 经办人请求头。
        vid: 版本 ID。
        submit: 是否继续提交到待复核状态。
    出参:
        无返回值。
    失败时:
        任一步失败由 call 抛 RuntimeError，事务已在服务端回滚。
    """
    call(client, "POST", f"/api/v1/changes/{vid}/research/run", op,  # 运行 harness
         {"field_answers": FIELD_ANSWER})                   # 现场答复
    call(client, "POST", f"/api/v1/changes/{vid}/research/confirm", op,  # 人工确认
         {"confirm": True, "note": "演示数据：已核对书面材料"})  # 确认备注
    if submit:                                               # 需要进入复核时
        call(client, "POST", f"/api/v1/changes/{vid}/submit", op)  # 提交复核


def main() -> None:
    """生成覆盖全部状态的演示数据。

    入参:
        无。
    出参:
        无返回值，逐条打印已造出的状态。
    失败时:
        服务未启动或任一接口失败时抛异常并以非零码退出。
    """
    # 为什么卡在这里：本机常设 HTTP_PROXY，若让 httpx 继承环境代理，
    # 同一 keep-alive 连接上的后续 POST 会被代理转发到错误的上游，表现为莫名其妙的 404。
    with httpx.Client(base_url=BASE, timeout=30.0, trust_env=False) as client:  # 回环请求禁用环境代理
        health = client.get("/health")                       # 先探活
        if health.status_code != 200:                        # 服务不可用
            raise RuntimeError(f"服务未就绪：{BASE} 返回 {health.status_code}")  # 明确失败
        op = token(client, "u_operator")                     # 经办人令牌
        ra = token(client, "u_reviewer_a")                   # 第一复核人令牌
        rb = token(client, "u_reviewer_b")                   # 第二复核人令牌
        ad = token(client, "u_admin")                        # 管理员令牌

        v = create(client, op, 2200000, "年度绩效调薪（全流程完成，已生效）")  # 场景一：已生效
        prepare(client, op, v)                               # 调研、确认、提交
        call(client, "POST", f"/api/v1/changes/{v}/review", ra, {"decision": "APPROVE", "comment": "材料齐备"})  # 第一签
        call(client, "POST", f"/api/v1/changes/{v}/review", rb, {"decision": "APPROVE", "comment": "同意"})  # 第二签
        call(client, "POST", f"/api/v1/changes/{v}/effective", ad, {"note": "演示数据生效"})  # 管理员生效
        print("[demo] EFFECTIVE 已生效（含完整双人复核轨迹）")  # 打印状态

        v = create(client, op, 2350000, "职级晋升调薪（双签齐备，待管理员生效）")  # 场景二：待生效
        prepare(client, op, v)                               # 前置流程
        call(client, "POST", f"/api/v1/changes/{v}/review", ra, {"decision": "APPROVE", "comment": "依据充分"})  # 第一签
        call(client, "POST", f"/api/v1/changes/{v}/review", rb, {"decision": "APPROVE", "comment": "同意"})  # 第二签
        print("[demo] APPROVED 双签齐备")                     # 打印状态

        v = create(client, op, 2100000, "岗位调整调薪（第一签已通过，待第二签）")  # 场景三：一签
        prepare(client, op, v)                               # 前置流程
        call(client, "POST", f"/api/v1/changes/{v}/review", ra, {"decision": "APPROVE", "comment": "待第二人复核"})  # 第一签
        print("[demo] REVIEW_ONE 已一签")                     # 打印状态

        v = create(client, op, 2050000, "专项奖励调薪（待第一复核人）")  # 场景四：待复核
        prepare(client, op, v)                               # 前置流程含提交
        print("[demo] PENDING_REVIEW 待第一签")              # 打印状态

        v = create(client, op, 2300000, "市场调研调薪（结论已确认，待提交）")  # 场景五：结论已确认
        prepare(client, op, v, submit=False)                # 只到人工确认，不提交
        print("[demo] RESEARCH_DONE 结论已确认")             # 打印状态

        v = create(client, op, 2400000, "新员工定薪调整（草稿，可直接发起调研）")  # 场景六：草稿
        print("[demo] DRAFT 草稿")                           # 打印状态

        v = create(client, op, 2500000, "缺少书面依据的调薪（已驳回）")  # 场景七：驳回
        prepare(client, op, v)                               # 前置流程
        call(client, "POST", f"/api/v1/changes/{v}/review", ra, {"decision": "REJECT", "comment": "缺少绩效书面依据"})  # 驳回
        print("[demo] REJECTED 已驳回")                      # 打印状态

        total = call(client, "GET", "/api/v1/changes", ad)  # 用管理员视角读回全部版本
        print(f"[demo] 完成：共 {len(total)} 个版本，覆盖 7 种状态")  # 打印汇总


if __name__ == "__main__":                                  # 只在直接执行时运行
    try:                                                    # 统一捕获并输出错误
        main()                                              # 执行造数
    except Exception as e:                                  # 任何失败都明确报出
        print(f"[demo] FAILED: {e}", file=sys.stderr)       # 写到标准错误
        sys.exit(1)                                         # 非零退出，便于判断
