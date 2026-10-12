"""Settings for the isolated Kubernetes live-test deployment only."""
from buro_first.settings import *
import os

SECRET_KEY = os.environ['DJANGO_SECRET_KEY']
DEBUG = False
ALLOWED_HOSTS = ['192.168.1.99', '127.0.0.1', 'localhost']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': '/data/db.sqlite3'}}
MEDIA_ROOT = '/data/media'
