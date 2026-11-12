"""统一对外响应结构与全局异常处理：所有接口都必须经过本模块返回。"""

from typing import Any                                          # 标注 data 可以是任意可序列化对象

from fastapi import FastAPI, Request                            # FastAPI 应用与请求对象，用于注册异常处理器
from fastapi.exceptions import RequestValidationError          # 请求体校验失败异常，需要转成统一结构
from fastapi.responses import JSONResponse                     # 统一以 JSON 返回，避免默认纯文本错误体
from starlette.exceptions import HTTPException as StarletteHTTPException  # 路由不存在等框架级异常


class BizError(Exception):
    """业务异常：携带稳定错误码，由全局处理器转成统一响应体。"""  # 每个类同样给出中文说明

    def __init__(self, code: str, msg: str, status_code: int = 400) -> None:
        """构造业务异常。

        入参:
            code: 稳定大写错误码，前端据此做分支，例如 REVIEW_SAME_PERSON。
            msg: 面向人的中文提示，不含堆栈与敏感数值。
            status_code: HTTP 状态码，默认 400；鉴权类传 401/403，冲突类传 409。
        出参:
            无返回值，初始化异常实例。
        失败时:
            不校验 code 格式；调用方应保证其非空。
        """
        self.code = code                                        # 保存错误码，供处理器写入 error.code
        self.msg = msg                                          # 保存提示文案，供处理器写入 error.msg
        self.status_code = status_code                          # 保存 HTTP 状态码
        super().__init__(msg)                                   # 调用基类构造，保证 str(e) 可读


def ok(data: Any = None) -> dict:
    """构造成功响应体。

    入参:
        data: 业务数据，字典、列表或 None 均可。
    出参:
        固定结构 {"ok": True, "data": ..., "error": {"code": "", "msg": ""}}。
    失败时:
        不会失败；data 不可序列化时由 JSONResponse 在响应阶段抛错，属编程错误需修复。
    """
    return {"ok": True, "data": data, "error": {"code": "", "msg": ""}}  # error 保持对象形状，结构恒定


def fail(code: str, msg: str) -> dict:
    """构造失败响应体。

    入参:
        code: 稳定大写错误码。
        msg: 面向人的中文提示。
    出参:
        固定结构 {"ok": False, "data": None, "error": {"code": code, "msg": msg}}。
    失败时:
        不会失败；调用方负责保证 code/msg 为字符串。
    """
    return {"ok": False, "data": None, "error": {"code": code, "msg": msg}}  # data 固定为 None，客户端无需猜测


def install_handlers(app: FastAPI) -> None:
    """注册全局异常处理器，保证任何失败路径都返回统一结构。

    入参:
        app: FastAPI 应用实例。
    出参:
        无返回值，就地注册处理器。
    失败时:
        不抛异常；若处理器自身写错，异常会在请求期暴露为 500，需在自测中覆盖。
    """

    @app.exception_handler(BizError)                            # 业务异常：正常可预期的失败分支
    async def _biz_handler(request: Request, exc: BizError) -> JSONResponse:
        """把 BizError 转成统一响应。

        入参:
            request: 当前请求，此处不读取内容，仅为满足处理器签名。
            exc: 被抛出的业务异常。
        出参:
            JSONResponse，状态码取自 exc.status_code。
        失败时:
            不额外抛错；错误信息不含堆栈，避免泄露内部实现。
        """
        return JSONResponse(status_code=exc.status_code, content=fail(exc.code, exc.msg))  # 统一封装，不裸抛

    @app.exception_handler(RequestValidationError)             # 请求体字段缺失/类型错误
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """把参数校验失败转成统一响应。

        入参:
            request: 当前请求。
            exc: Pydantic 校验异常。
        出参:
            422 状态码 + 统一失败结构。
        失败时:
            exc.errors() 可能含不可序列化对象，故只取字段路径与提示文字，不回传原始输入值。
        """
        brief = "; ".join(                                      # 拼接成一句可读提示
            f"{'.'.join(str(x) for x in e.get('loc', []))}:{e.get('msg', '')}"  # 只取位置和原因
            for e in exc.errors()                              # 遍历全部校验错误
        )                                                      # 拼接结束
        return JSONResponse(status_code=422, content=fail("VALIDATION_ERROR", brief))  # 不回显用户提交的敏感值

    @app.exception_handler(StarletteHTTPException)            # 404/405 等框架级 HTTP 异常
    async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """把框架 HTTP 异常转成统一响应。

        入参:
            request: 当前请求。
            exc: Starlette 抛出的 HTTP 异常。
        出参:
            对应状态码 + 统一失败结构。
        失败时:
            detail 非字符串时统一转成"请求不合法"，避免非字符串体破坏结构。
        """
        msg = exc.detail if isinstance(exc.detail, str) else "请求不合法"  # 只接受字符串，保持 msg 类型稳定
        return JSONResponse(status_code=exc.status_code, content=fail("HTTP_ERROR", msg))  # 统一封装

    @app.exception_handler(Exception)                         # 兜底：任何未预期异常都不把堆栈返回给调用方
    async def _unknown_handler(request: Request, exc: Exception) -> JSONResponse:
        """兜底异常处理。

        入参:
            request: 当前请求。
            exc: 任意未捕获异常。
        出参:
            500 状态码 + 统一失败结构。
        失败时:
            不返回 exc 文本；堆栈只由服务端日志记录，防止内部路径、SQL、密钥外泄。
        """
        return JSONResponse(status_code=500, content=fail("INTERNAL_ERROR", "服务内部错误，请联系管理员"))  # 固定文案
