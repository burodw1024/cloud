from agentic_ai.tests.settings import *
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.admin', 'chat']
ROOT_URLCONF = 'chat.tests.employee_urls'
MIDDLEWARE = [*MIDDLEWARE, 'django.contrib.messages.middleware.MessageMiddleware']
TEMPLATES[0]['OPTIONS']['context_processors'].append('django.contrib.messages.context_processors.messages')
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
