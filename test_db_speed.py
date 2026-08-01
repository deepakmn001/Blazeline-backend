import time
import django
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import connection

t0 = time.time()
with connection.cursor() as c:
    c.execute("SELECT 1")
print("First query:", time.time() - t0)

t0 = time.time()
with connection.cursor() as c:
    c.execute("SELECT 1")
print("Second query:", time.time() - t0)

t0 = time.time()
with connection.cursor() as c:
    c.execute("SELECT 1")
print("Third query:", time.time() - t0)