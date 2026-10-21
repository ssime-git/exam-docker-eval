"""Routes renommées et exception du service (464154).

    uvx --with testcontainers --with docker --with requests --with pyyaml python test_routes_renommees.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from docker_eval.bentoml_runner import derniere_exception, routes_renommees  # noqa: E402

assert routes_renommees(["/login_service", "/predict_service"]) == {"/login": "/login_service", "/predict": "/predict_service"}
assert routes_renommees(["/login", "/predict"]) == {}
assert routes_renommees(["/login_a", "/login_b", "/predict"]) == {}, "ambigu : rien"
assert routes_renommees(["/healthz", "/predict_service"]) == {}, "pas de connexion : rien"

journaux = """[ERROR] Exception on /predict_service [POST]
Traceback (most recent call last):
  File "/usr/local/lib/python3.11/site-packages/_bentoml_impl/server/app.py", line 600, in api_endpoint
    output = await self._to_thread(func, *input_args, **input_params)
  File "/home/bentoml/bento/src/src/service.py", line 117, in predict_service
    if not auth.startswith("Bearer "):
AttributeError: 'ServiceContext' object has no attribute 'startswith'
"""
assert derniere_exception(journaux) == {"message": "AttributeError: 'ServiceContext' object has no attribute 'startswith'",
                                         "lieu": "src/service.py:117", "fonction": "predict_service"}
assert derniere_exception("aucune trace") == {}
print("test_routes_renommees : ok")
