"""API endpoints for managing adaptive iframe domain unlock rules."""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.security import authenticate_http_request
from app.models.unlock_rule import (
    DomainUnlockRule,
    DomainUnlockRuleCreate,
    DomainUnlockRulesResponse,
)
from app.services.unlock_rules_service import unlock_rules_service
from app.core.logger import logger

router = APIRouter(prefix="/unlock-rules", tags=["Unlock Rules"])


@router.get("", response_model=DomainUnlockRulesResponse, dependencies=[Depends(authenticate_http_request)])
async def get_unlock_rules():
    """Returns all learned domain unlock rules. Strictly authenticated via API_TOKEN."""
    rules = unlock_rules_service.get_all_rules()
    return DomainUnlockRulesResponse(rules=rules, total=len(rules))


@router.get("/{domain}", response_model=DomainUnlockRule, dependencies=[Depends(authenticate_http_request)])
async def get_domain_unlock_rule(domain: str):
    """Returns the unlock rule for a specific domain. Strictly authenticated via API_TOKEN."""
    rule = unlock_rules_service.get_rule(domain)
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No unlock rule found for domain '{domain}'",
        )
    return rule


@router.post("", response_model=DomainUnlockRule, status_code=status.HTTP_201_CREATED, dependencies=[Depends(authenticate_http_request)])
async def upsert_unlock_rule(rule_data: DomainUnlockRuleCreate):
    """Registers or updates a domain unlock rule using closed whitelists for headers and patches."""
    logger.info(
        f"Registering unlock rule for '{rule_data.domain}': "
        f"headers={rule_data.headers_stripped}, patches={rule_data.js_patches}"
    )
    rule = unlock_rules_service.upsert_rule(rule_data)
    return rule


@router.post("/{domain}/report-failure", response_model=DomainUnlockRule, dependencies=[Depends(authenticate_http_request)])
async def report_unlock_failure(domain: str):
    """Reports a rendering/navigation failure on a domain, triggering adaptive re-learning."""
    logger.warning(f"Reporting render/unlock failure on domain '{domain}'. Flipping to stale_relearning.")
    rule = unlock_rules_service.record_failure(domain)
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Domain '{domain}' was not registered in unlock rules.",
        )
    return rule
