"""阶段④验证脚本：验证前端单页可访问、零外部依赖、无硬编码口令与同源接口连通。"""

import os                                                       # 注入运行期环境变量
import sys                                                      # 把项目根目录加入模块搜索路径
import tempfile                                                 # 创建隔离的临时数据库目录
from pathlib import Path                                        # 读取前端文件与处理路径

ROOT = Path(__file__).resolve().parent.parent                   # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))                                   # 保证能 import app 包
_TMP = tempfile.TemporaryDirectory(prefix="wb_stage4_")       # 临时目录，退出时自动清理
os.environ["APP_TOKEN_SECRET"] = "stage4-only-secret"          # 令牌签名密钥
os.environ["APP_PIN_OPERATOR"] = "pin-op"                      # 经办人口令
os.environ["APP_PIN_REVIEWER_A"] = "pin-a"                     # 第一复核人口令
os.environ["APP_PIN_REVIEWER_B"] = "pin-b"                     # 第二复核人口令
os.environ["APP_PIN_ADMIN"] = "pin-admin"                      # 管理员口令
os.environ["APP_LLM_MODE"] = "mock"                            # 离线模型模式
os.environ["APP_DB_PATH"] = str(Path(_TMP.name) / "stage4.db")  # 临时库

from fastapi.testclient import TestClient                      # 进程内客户端
from app.config import load_config, resolve_path              # 读取配置定位前端文件
from app.main import app                                       # 被测 FastAPI 应用


def main() -> None:
    """执行阶段④全部断言。

    入参:
        无。
    出参:
        无返回值，逐条打印 PASS。
    失败时:
        任一断言不成立时抛 AssertionError 并以非零码退出。
    """
    ui_path = resolve_path(load_config()["ui"]["file"])       # 从配置解析前端文件路径
    html = ui_path.read_text(encoding="utf-8")                # 读取前端源码
    assert "<script>" in html and "</script>" in html         # 必须含内联脚本
    assert "<style>" in html and "</style>" in html           # 必须含内联样式
    assert "http://" not in html and "https://" not in html   # 零外部资源，页面可离线渲染
    for word in ("pin-op", "pin-a", "pin-b", "pin-admin", "stage4-only-secret"):  # 常见演示口令与密钥
        assert word not in html, "前端不得硬编码任何口令或密钥：" + word  # 逐个确认未出现
    css = html.split("<style>")[1].split("</style>")[0]      # 取出 CSS 段
    bad_lines = [i + 1 for i, line in enumerate(css.splitlines())  # 检查注释是否成对
                 if ("/*" in line) != ("*/" in line)]        # 只统计不配对的行号
    assert not bad_lines, f"CSS 注释未闭合：{bad_lines}"      # 未闭合会吞掉后续样式
    for fn in ("doLogin", "runResearch", "confirmResearch", "doReview",  # 前端必备交互函数
               "effectiveChange", "openDetail", "searchKB"):  # 继续清单
        assert fn in html, f"前端缺少函数：{fn}"              # 逐个确认存在
    print("[stage4] PASS 前端单页零外部依赖、无硬编码口令、样式与交互完整")  # 打印通过项

    with TestClient(app) as client:                            # 触发 lifespan
        root = client.get("/", follow_redirects=False)        # 根路径不自动跟随跳转
        assert root.status_code in (302, 307)                 # 必须是重定向
        assert root.headers.get("location") == "/ui"          # 指向前端入口
        page = client.get("/ui")                             # 请求前端页面
        assert page.status_code == 200                        # 页面可访问
        assert "text/html" in page.headers["content-type"]    # 响应类型正确
        assert "薪酬变更版本与双人复核" in page.text          # 中文内容未乱码
        assert "charset=utf-8" in page.headers["content-type"]  # 显式声明中文编码
        print("[stage4] PASS / → /ui 跳转与页面同源返回")    # 打印通过项

        token = client.post("/api/v1/auth/token",             # 走前端同款登录请求
                            json={"actor_id": "u_operator", "pin": "pin-op"})  # 账号与口令
        assert token.status_code == 200 and token.json()["ok"] is True  # 登录成功
        hdr = {"Authorization": "Bearer " + token.json()["data"]["token"]}  # 组装请求头
        created = client.post("/api/v1/changes", headers=hdr,  # 走前端同款新建版本请求
                              json={"emp_id": "E001", "new_base_salary_cents": 2200000,  # 金额（分）
                                    "reason": "前端联调自测", "research_question": "核实取证要点"})  # 事由与问题
        assert created.status_code == 200 and created.json()["ok"] is True  # 创建成功
        listing = client.get("/api/v1/changes", headers=hdr)  # 走前端同款列表请求
        assert listing.status_code == 200 and len(listing.json()["data"]) == 1  # 列表可见
        assert listing.json()["data"][0]["after"]["base_salary_display"] == "****"  # 前端拿到的是脱敏值
        docs = client.get("/docs")                           # FastAPI 自带接口文档仍在
        assert docs.status_code == 200                       # 未被前端路由破坏
        print("[stage4] PASS 前端同源接口联调（登录 / 新建 / 列表 / 脱敏）")  # 打印通过项

    _TMP.cleanup()                                         # 清理临时数据库目录
    print("[stage4] ALL PASS")                             # 全部通过


if __name__ == "__main__":                                  # 只在直接执行时运行
    main()                                                  # 调用主函数
