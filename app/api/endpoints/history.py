from fastapi import APIRouter, Depends, Query, Request
from sqlmodel import Session, select, desc
from app.core.db import get_session
from app.models.db import UsageSnapshot
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from app.core.rate_limit import limiter

router = APIRouter()

@router.get("/")
@limiter.limit("30/minute")
async def get_usage_history(
    request: Request,
    provider_id: Optional[str] = None,
    account_id: Optional[str] = None,
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session)
) -> List[Dict[str, Any]]:
    """Fetch usage history snapshots."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    
    statement = select(UsageSnapshot).where(UsageSnapshot.timestamp >= since)
    
    if provider_id:
        statement = statement.where(UsageSnapshot.provider_id == provider_id)
    if account_id:
        statement = statement.where(UsageSnapshot.account_id == account_id)
        
    statement = statement.order_by(desc(UsageSnapshot.timestamp)).limit(limit)
    
    results = session.exec(statement).all()
    
    # Process snapshots to include decrypted metadata and flat structure for UI
    history = []
    for s in results:
        history.append({
            "id": s.id,
            "timestamp": s.timestamp.isoformat(),
            "provider_id": s.provider_id,
            "account_id": s.account_id,
            "account_label": s.account_label,
            "service_name": s.service_name,
            "used_value": s.used_value,
            "limit_value": s.limit_value,
            "unit_type": s.unit_type,
            "currency": s.currency,
            "tier": s.tier,
            "model_id": s.model_id,
            "window_type": s.window_type,
            "health": s.health,
            "sidecar_id": s.sidecar_id,
            "is_unlimited": s.is_unlimited,
            "data_source": s.data_source,
            "metadata": s.raw_metadata
        })
        
    return history
