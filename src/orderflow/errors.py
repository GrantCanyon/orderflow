class DomainError(Exception):
    def __init__(self, code, message=None, details=None, status=409, retry_after=None):
        super().__init__(message or f"Операция отклонена ({code})")
        self.code = code
        self.details = details or {}
        self.status = status
        self.retry_after = retry_after


def storage_error(error):
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    original = getattr(error, "orig", None)
    sqlstate = getattr(original, "sqlstate", None)
    sqlite_code = getattr(original, "sqlite_errorcode", 0) or 0
    if sqlstate == "55P03" or (sqlite_code & 255) in {5, 6} or isinstance(error, PoolTimeout):
        code, message = (
            "storage_busy",
            "Хранилище занято. Повторите создание с тем же ключом запроса.",
        )
    elif sqlstate == "40P01":
        code, message = (
            "transaction_retry",
            "Операция прервана из-за конкуренции. Повторите её с тем же ключом.",
        )
    elif getattr(error, "connection_invalidated", False) or (
        sqlstate and sqlstate.startswith("08")
    ):
        code, message = (
            "storage_unavailable",
            "Связь с хранилищем прервана. Повторите создание с тем же ключом.",
        )
    else:
        return DomainError(
            "storage_error",
            "Не удалось выполнить операцию с хранилищем. "
            "При повторе создания используйте тот же ключ.",
            {"retryable": False},
            status=500,
        )
    return DomainError(code, message, {"retryable": True}, status=503, retry_after=1)
