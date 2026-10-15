"""Le corps envoyé à /predict suit le contrat que la copie déclare.

    uvx --with testcontainers --with docker --with requests --with pyyaml python test_contrat_predict.py

458998 : service FastAPI monté dans BentoML, `def predict(data: AdmissionInput,
payload: dict = Depends(verify_token))`. Le moteur ne lisait que la forme
BentoML `def predict(self, x: T)` et envoyait ses corps de repli (GRE,
TOEFL… ou en minuscules) : 422 sur une copie qui marche.
"""
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from docker_eval.bentoml_runner import BentoMLRunner  # noqa: E402

SERVICE_458998 = '''
from pydantic import BaseModel
from fastapi import FastAPI, Depends, HTTPException, Header
import bentoml

USERS_DB = {"admin": "admin123"}
fastapi_app = FastAPI()

class LoginInput(BaseModel):
    username: str
    password: str

class AdmissionInput(BaseModel):
    GRE_Score: float
    TOEFL_Score: float
    University_Rating: float
    SOP: float
    LOR: float
    CGPA: float
    Research: int

def verify_token(authorization: str = Header(None)):
    return {}

@fastapi_app.post("/login")
def login(data: LoginInput):
    return {}

@fastapi_app.post("/predict")
def predict(data: AdmissionInput, payload: dict = Depends(verify_token)):
    return {}

@bentoml.mount_asgi_app(fastapi_app, path="/")
class AdmissionService:
    pass
'''

SERVICE_BENTOML = '''
import bentoml
from pydantic import BaseModel

class Features(BaseModel):
    gre_score: float
    cgpa: float

@bentoml.service
class Service:
    @bentoml.api
    def predict(self, input_data: Features) -> dict:
        return {}
'''


def contrat(service: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "src"))
        with open(os.path.join(d, "src", "service.py"), "w") as f:
            f.write(service)
        return BentoMLRunner("t", d, 60, logging.getLogger("t"))._discover_api_contract()


champs = {"GRE_Score", "TOEFL_Score", "University_Rating", "SOP", "LOR", "CGPA", "Research"}
c = contrat(SERVICE_458998)
assert set(c["predict_bodies"][0]) == champs, c["predict_bodies"][0]
# FastAPI, un seul modèle en corps : pas d'enveloppe au nom du paramètre
assert "data" not in c["predict_wrappers"], c["predict_wrappers"]
assert c["predict_wrappers"][0] is None, c["predict_wrappers"]
print("OK : route FastAPI, corps à plat avec les champs du modèle (458998)")

# BentoML : le comportement d'avant ne bouge pas (enveloppe au nom du paramètre)
c = contrat(SERVICE_BENTOML)
assert set(c["predict_bodies"][0]) == {"gre_score", "cgpa"}, c["predict_bodies"][0]
assert c["predict_wrappers"][0] == "input_data", c["predict_wrappers"]
print("OK : service BentoML, enveloppe au nom du paramètre")
