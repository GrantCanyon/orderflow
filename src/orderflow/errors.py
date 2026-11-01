class DomainError(Exception):
    def __init__(self, code, message=None, details=None, status=409):
        super().__init__(message or f"Операция отклонена ({code})")
        self.code = code
        self.details = details or {}
        self.status = status
