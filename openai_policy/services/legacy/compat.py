from __future__ import annotations

class HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        self.status_code = int(status_code)
        self.detail = detail
        super().__init__(str(detail))

class APIRouter:
    def __init__(self, *args, **kwargs):
        pass
    def _decorator(self, *args, **kwargs):
        def wrapper(func):
            return func
        return wrapper
    get = _decorator
    post = _decorator
    delete = _decorator
    put = _decorator
    patch = _decorator

def File(default=..., *args, **kwargs): return default
def Form(default=..., *args, **kwargs): return default
def Header(default=None, *args, **kwargs): return default
def Field(default=None, *args, **kwargs): return default

class BaseModel:
    def __init__(self, **kwargs):
        annotations = {}
        for cls in reversed(type(self).__mro__):
            annotations.update(getattr(cls, "__annotations__", {}) or {})
        for name in annotations:
            if name in kwargs:
                value = kwargs[name]
            elif hasattr(type(self), name):
                value = getattr(type(self), name)
            else:
                value = None
            setattr(self, name, value)
        for name, value in kwargs.items():
            setattr(self, name, value)

class BackgroundTasks:
    def __init__(self): self.tasks = []
    def add_task(self, func, *args, **kwargs): self.tasks.append((func, args, kwargs))

class UploadFile:
    def __init__(self, file, filename="", content_type="", headers=None):
        self.file = file
        self.filename = filename
        self.content_type = content_type or "application/octet-stream"
        self.headers = headers

class FileResponse:
    def __init__(self, path, filename=None, *args, **kwargs):
        self.path = path
        self.filename = filename

class PlainTextResponse:
    def __init__(self, content, media_type=None, *args, **kwargs):
        self.content = content
        self.media_type = media_type

class _LazyOpenAI:
    """Resolve OpenAI settings dynamically from the existing SystemSetting dashboard."""
    def __init__(self, *args, **kwargs):
        self._client = None
        self._signature = None
        self._args = args
        self._kwargs = dict(kwargs)

    def _get(self):
        from openai import OpenAI as RealOpenAI
        from chat.dynamic_config import get_setting

        kwargs = dict(self._kwargs)
        api_key = str(
            kwargs.get("api_key") or get_setting("OPENAI_API_KEY", "") or ""
        ).strip()
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not configured. Set it in /admin-tools/settings/."
            )

        base_url = str(
            kwargs.get("base_url") or get_setting("OPENAI_BASE_URL", "") or ""
        ).strip()
        signature = (api_key, base_url)

        if self._client is None or self._signature != signature:
            client_kwargs = dict(kwargs)
            client_kwargs["api_key"] = api_key
            if base_url:
                client_kwargs["base_url"] = base_url
            else:
                client_kwargs.pop("base_url", None)
            self._client = RealOpenAI(*self._args, **client_kwargs)
            self._signature = signature

        return self._client

    def __getattr__(self, name):
        return getattr(self._get(), name)

OpenAI = _LazyOpenAI
