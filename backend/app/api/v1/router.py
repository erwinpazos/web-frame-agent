from fastapi import APIRouter
from app.api.v1.endpoints.agent import router as agent_router
from app.api.v1.endpoints.ws import router as ws_router
from app.api.v1.endpoints.cdp_bridge import router as cdp_router
from app.api.v1.endpoints.unlock_rules import router as unlock_rules_router
from app.api.v1.endpoints.auth import router as auth_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(agent_router)
api_router.include_router(ws_router)
api_router.include_router(cdp_router)
api_router.include_router(unlock_rules_router)
