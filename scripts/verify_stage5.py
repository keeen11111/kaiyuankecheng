"""阶段⑤验证脚本：列表筛选、历史版本链、驳回后重建草稿。"""

import os                                                       # 注入运行期环境变量
import sys                                                      # 把项目根目录加入模块搜索路径
import tempfile                                                 # 创建隔离的临时数据库目录
from pathlib import Path                                        # 处理跨平台路径

ROOT = Path(__file__).resolve().parent.parent                   # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))                                   # 保证能 import app 包
_TMP = tempfile.TemporaryDirectory(prefix="wb_stage5_")        # 临时目录，退出时自动清理
os.environ["APP_TOKEN_SECRET"] = "stage5-only-secret"           # 令牌签名密钥
os.environ["APP_PIN_OPERATOR"] = "pin-op"                       # 经办人口令
os.environ["APP_PIN_REVIEWER_A"] = "pin-a"                      # 第一复核人口令
os.environ["APP_PIN_REVIEWER_B"] = "pin-b"                      # 第二复核人口令
os.environ["APP_PIN_ADMIN"] = "pin-admin"                       # 管理员口令
os.environ["APP_LLM_MODE"] = "mock"                             # 离线模型模式
os.environ["APP_DB_PATH"] = str(Path(_TMP.name) / "stage5.db")  # 临时库

from fastapi.testclient import TestClient                      # 进程内客户端
from app.main import app                                       # 被测 FastAPI 应用

FIELD_ANSWERS = {                                              # 现场书面答复，键与 mock 模型提问一致
    "请确认调薪生效月份、绩效书面依据及员工是否知悉": "生效月份为次月，绩效结论有书面评级表，员工已签字知悉。",  # 答复正文
}                                                              # 字典结束


def auth(client: TestClient, actor_id: str, pin: str) -> dict:
    """签发令牌并返回请求头字典。

    入参:
        client: TestClient 实例。
        actor_id: 账号 ID。
        pin: 账号口令。
    出参:
        {"Authorization": "Bearer xxx"}。
    失败时:
        签发失败抛 AssertionError，并带响应体便于定位。
    """
    r = client.post("/api/v1/auth/token", json={"actor_id": actor_id, "pin": pin})  # 调签发接口
    assert r.status_code == 200 and r.json()["ok"] is True, r.text  # 必须成功
    return {"Authorization": "Bearer " + r.json()["data"]["token"]}  # 组装请求头


def create(client: TestClient, hdr: dict, cents: int, reason: str) -> str:
    """创建一个变更版本并返回版本 ID。

    入参:
        client: TestClient 实例。
        hdr: 经办人请求头。
        cents: 变更后月薪（分）。
        reason: 变更事由。
    出参:
        版本 ID 字符串。
    失败时:
        创建失败抛 AssertionError 并带响应体。
    """
    r = client.post("/api/v1/changes", headers=hdr,             # 调创建接口
                    json={"emp_id": "E001", "new_base_salary_cents": cents,  # 金额
                          "reason": reason,                     # 事由
                          "research_question": "核实岗位职级、绩效依据与生效月份"})  # 调研问题
    assert r.status_code == 200 and r.json()["ok"] is True, r.text  # 必须成功
    return r.json()["data"]["version_id"]                       # 返回版本 ID


def prepare_and_reject(client: TestClient, hdr: dict, cents: int, reason: str) -> str:
    """把版本一路推到待复核，再由复核人驳回，返回 REJECTED 版本 ID。

    入参:
        client: TestClient 实例。
        hdr: 经办人请求头。
        cents: 变更后月薪（分）。
        reason: 变更事由。
    出参:
        已驳回的版本 ID。
    失败时:
        任一步状态或返回不符时抛 AssertionError，说明状态机被改坏。
    """
    vid = create(client, hdr, cents, reason)                    # 先建草稿
    r1 = client.post(f"/api/v1/changes/{vid}/research/run", headers=hdr,  # 跑 harness
                     json={"field_answers": FIELD_ANSWERS})      # 传现场答复
    assert r1.status_code == 200 and r1.json()["ok"] is True, r1.text  # 调研成功
    r2 = client.post(f"/api/v1/changes/{vid}/research/confirm", headers=hdr,  # 人工确认
                     json={"confirm": True, "note": "已核对书面材料"})  # 显式确认
    assert r2.status_code == 200 and r2.json()["ok"] is True, r2.text  # 确认成功
    r3 = client.post(f"/api/v1/changes/{vid}/submit", headers=hdr)  # 提交复核
    assert r3.status_code == 200 and r3.json()["ok"] is True, r3.text  # 提交成功
    ra = auth(client, "u_reviewer_a", "pin-a")                  # 复核甲登录
    r4 = client.post(f"/api/v1/changes/{vid}/review", headers=ra,  # 第一签直接驳回
                     json={"decision": "REJECT", "comment": "绩效依据不足"})  # 驳回意见
    assert r4.status_code == 200 and r4.json()["ok"] is True, r4.text  # 驳回成功
    return vid                                                  # 返回驳回版本


def main() -> None:
    """执行阶段⑤全部断言。

    入参:
        无。
    出参:
        无返回值，逐条打印 PASS。
    失败时:
        任一断言不成立时抛 AssertionError 并以非零码退出。
    """
    with TestClient(app) as client:                             # 触发 lifespan
        op = auth(client, "u_operator", "pin-op")               # 经办人身份
        v1 = create(client, op, 2100000, "绩效调薪 A 档")        # 版本一
        v2 = create(client, op, 2300000, "岗位晋升调薪 B 档")    # 版本二

        only_draft = client.get("/api/v1/changes?status=DRAFT", headers=op)  # 按状态筛选
        assert only_draft.status_code == 200 and len(only_draft.json()["data"]) == 2  # 两条草稿都在
        by_emp = client.get("/api/v1/changes?emp_id=E999", headers=op)  # 按不存在工号筛选
        assert len(by_emp.json()["data"]) == 0                  # 必须为空
        by_kw = client.get("/api/v1/changes?q=晋升", headers=op)  # 按关键词筛选
        assert len(by_kw.json()["data"]) == 1                   # 只命中版本二
        assert by_kw.json()["data"][0]["version_id"] == v2       # 命中的确实是版本二
        bad = client.get("/api/v1/changes?status=NOT_A_STATUS", headers=op)  # 非法状态值
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_STATUS_FILTER"  # 明确报错
        assert bad.json()["data"] is None                        # 失败时 data 必须为 null
        print("[stage5] PASS 列表支持状态 / 工号 / 关键词筛选，非法状态被拒")  # 打印通过项

        chain = client.get(f"/api/v1/changes/{v1}/chain", headers=op)  # 查版本链
        body = chain.json()["data"]                          # 取数据体
        assert chain.status_code == 200 and body["emp_id"] == "E001"  # 工号正确
        assert body["current"]["version_no"] == 1                # 当前基线版本号为 1
        assert body["current"]["base_salary_display"] == "****"  # 非管理员看不到明文金额
        assert len(body["items"]) == 2                           # 两个版本都在链上
        assert sum(1 for i in body["items"] if i["is_current"]) == 1  # 只有锚点版本被标记为当前
        assert body["items"][0]["created_at"] <= body["items"][1]["created_at"]  # 升序排列
        assert all(i["after_display"] == "****" for i in body["items"])  # 全链金额脱敏
        print("[stage5] PASS 历史版本链含当前基线、升序排列、非管理员全脱敏")  # 打印通过项

        blocked = client.post(f"/api/v1/changes/{v1}/clone", headers=op, json={})  # 草稿态不允许克隆
        assert blocked.status_code == 409                        # 必须被拒
        assert blocked.json()["error"]["code"] == "INVALID_STATUS"  # 错误码明确
        print("[stage5] PASS 非驳回状态禁止复制，避免产生平行版本")  # 打印通过项

        rejected = prepare_and_reject(client, op, 2600000, "需补充绩效依据的调薪")  # 造一个驳回版本
        reopened = client.post(f"/api/v1/changes/{rejected}/clone", headers=op,  # 基于驳回版本重建
                               json={"new_base_salary_cents": 2650000})  # 重新确认金额
        assert reopened.status_code == 200 and reopened.json()["ok"] is True, reopened.text  # 重建成功
        new_ver = reopened.json()["data"]                        # 取新版本视图
        assert new_ver["status"] == "DRAFT"                      # 新版本必须是草稿
        assert new_ver["source_version_id"] == rejected          # 回传来源版本
        assert new_ver["emp_id"] == "E001"                       # 同一工号
        assert "基于被驳回版本重建" in new_ver["reason"]         # 事由自动标注来源
        assert new_ver["after"]["base_salary_display"] == "****"  # 经办人看到的仍是脱敏值
        old = client.get(f"/api/v1/changes/{rejected}", headers=op).json()["data"]  # 回查源版本
        assert old["status"] == "REJECTED"                       # 源版本状态不变，审计链可追溯
        print("[stage5] PASS 驳回版本可重建为新草稿，源版本保持 REJECTED")  # 打印通过项

        admin = auth(client, "u_admin", "pin-admin")             # 管理员身份
        new_detail = client.get(f"/api/v1/changes/{new_ver['version_id']}", headers=admin).json()["data"]  # 管理员看明文
        assert new_detail["after"]["base_salary_cents"] == 2650000  # 金额按重建时输入写入
        assert new_detail["before"]["base_salary_cents"] == 2000000  # 变更前取的是最新基线，不是旧快照
        audit = client.get("/api/v1/audit?limit=200", headers=admin).json()["data"]  # 取审计流水
        clones = [i for i in audit["items"] if i["action"] == "CHANGE_CLONE"]  # 找重建记录
        assert clones and clones[0]["target"] == new_ver["version_id"]  # 审计指向新版本
        assert audit["chain"]["ok"] is True                      # 审计链仍然完整
        print("[stage5] PASS 重建后 before 取最新基线，审计留痕且链完整")  # 打印通过项

        refreshed_chain = client.get(f"/api/v1/changes/{new_ver['version_id']}/chain", headers=op)  # 链应包含三个版本
        assert len(refreshed_chain.json()["data"]["items"]) == 4  # 版本一、版本二、驳回版本、重建版本
        print("[stage5] PASS 重建后版本链自动纳入新草稿")        # 打印通过项

    _TMP.cleanup()                                              # 清理临时数据库目录
    print("[stage5] ALL PASS")                                  # 全部通过


if __name__ == "__main__":                                      # 只在直接执行时运行
    main()                                                      # 调用主函数
