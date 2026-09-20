"""Pydantic schemas for domain unlock rules and adaptive iframe configuration."""
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class AllowedStripHeader(str, Enum):
    """Rigid whitelist of headers permitted for anti-framing strip rules."""
    X_FRAME_OPTIONS = "x-frame-options"
    CONTENT_SECURITY_POLICY = "content-security-policy"
    CONTENT_SECURITY_POLICY_REPORT_ONLY = "content-security-policy-report-only"
    FRAME_OPTIONS = "frame-options"


class AllowedJsPatch(str, Enum):
    """Rigid whitelist of vetted named JS patches for anti-framebusting."""
    SPOOF_TOP_HIERARCHY = "spoof-top-hierarchy"
    HIDE_WEBDRIVER = "hide-webdriver"
    REWRITE_COOKIES_CHIPS = "rewrite-cookies-chips"
    CONTAIN_WINDOW_OPEN = "contain-window-open"
    STRIP_META_CSP = "strip-meta-csp"


class DomainUnlockStatus(str, Enum):
    """Status lifecycle of a domain recipe."""
    PROBING = "probing"
    UNLOCKED = "unlocked"
    STALE_RELEARNING = "stale_relearning"
    UNRECOVERABLE = "unrecoverable"


class DomainUnlockRuleCreate(BaseModel):
    """Schema for registering or updating a domain unlock rule."""
    domain: str = Field(..., min_length=1, max_length=253, description="Target domain (e.g. 'github.com')")
    headers_stripped: List[AllowedStripHeader] = Field(default_factory=list, description="Validated headers to strip")
    js_patches: List[AllowedJsPatch] = Field(default_factory=list, description="Validated JS patches to apply")
    status: DomainUnlockStatus = Field(default=DomainUnlockStatus.UNLOCKED, description="Status of the domain recipe")


class DomainUnlockRule(BaseModel):
    """Full domain unlock rule stored and returned by the API."""
    domain: str
    headers_stripped: List[AllowedStripHeader] = Field(default_factory=list)
    js_patches: List[AllowedJsPatch] = Field(default_factory=list)
    status: DomainUnlockStatus = DomainUnlockStatus.UNLOCKED
    success_count: int = 1
    failure_count: int = 0
    version: int = 1
    updated_at: float
    expires_at: float


class DomainUnlockRulesResponse(BaseModel):
    """Response containing all active domain unlock rules."""
    rules: List[DomainUnlockRule]
    total: int
