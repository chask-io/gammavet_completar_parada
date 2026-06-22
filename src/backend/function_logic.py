"""
Business logic for CompletarParadaFn.

This dedicated lambda has no accion parameter. Selecting the "Completar parada"
node means successful completion of the exact intended route stop.
"""

import logging
from typing import Any

import requests
from chask_foundation.backend.models import OrchestrationEvent

from backend.conductor_common import (
    ESTADO_COMPLETADO,
    TENANT_COMPLETE_CURRENT_PATH,
    TENANT_COMPLETE_CURRENT_ROUTE,
    ConductorContext,
    ConductorRuntime,
    tenant_data_public_test_mode,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RUNTIME = ConductorRuntime(
    actor_lambda="gammavet_completar_parada",
    function_uuid_default="00000000-0000-4000-8000-000000000001",
)


class FunctionBackend:
    """Complete the requested Gammavet route stop successfully."""

    def __init__(self, orchestration_event: OrchestrationEvent):
        self.orchestration_event = orchestration_event
        self.context = ConductorContext(orchestration_event, RUNTIME)
        logger.info(
            "CompletarParadaFn initialized for org: %s",
            orchestration_event.organization.organization_id,
        )

    def process_request(self) -> str:
        args = self.context.tool_args()
        nota = str(args.get("nota") or args.get("note") or "").strip()
        resolved = self.context.resolve_route_stop_ids()

        payload = self.context.build_driver_action_payload()
        if resolved.route_stop_id:
            payload["route_stop_id"] = resolved.route_stop_id
        if resolved.pickup_order_id:
            payload["pickup_order_id"] = resolved.pickup_order_id
        if nota:
            payload["note"] = nota

        logger.info(
            "CompletarParadaFn complete-current driver_id=%s driver_phone=%s route_stop_id=%s pickup_order_id=%s event_id=%s",
            payload.get("driver_id"),
            payload.get("driver_phone"),
            payload.get("route_stop_id"),
            payload.get("pickup_order_id"),
            payload["orchestration_event_uuid"],
        )

        try:
            with tenant_data_public_test_mode():
                result = self.context.tenant_client().post(
                    TENANT_COMPLETE_CURRENT_PATH,
                    json=payload,
                )
        except requests.HTTPError as exc:
            if self.context.is_no_active_stop_http_404(exc):
                return self.context.complete_current_missing_terminal(exc)
            raise

        if not isinstance(result, dict):
            raise RuntimeError(
                f"Tenant API {TENANT_COMPLETE_CURRENT_ROUTE} devolvio una respuesta inesperada"
            )

        completed_stop = result.get("route_stop") or {}
        if self.context.completion_response_mismatched(
            completed_stop,
            requested_route_stop_id=resolved.route_stop_id,
            requested_pickup_order_id=resolved.pickup_order_id,
        ):
            self.context.emit_completion_mismatch(
                completed_stop,
                requested_route_stop_id=resolved.route_stop_id,
                requested_pickup_order_id=resolved.pickup_order_id,
                outcome=ESTADO_COMPLETADO,
                endpoint_route=TENANT_COMPLETE_CURRENT_ROUTE,
            )
            return (
                "Bloqueada confirmacion de CompletarParadaFn por mismatch de "
                "route_stop_id/pickup_order_id en respuesta Tenant API."
            )

        return self._notify_completion(result, completed_stop)

    def _notify_completion(self, result: dict[str, Any], completed_stop: dict[str, Any]) -> str:
        num_actual = completed_stop.get("stop_number") or completed_stop.get("queue_position") or "?"
        total = result.get("total_stops") or completed_stop.get("total_stops") or "?"
        clinic_name = (
            completed_stop.get("clinic_name_snapshot")
            or completed_stop.get("clinica")
            or "parada actual"
        )

        if result.get("has_next_pending") or result.get("next_route_stop"):
            self.context.enviar_mensaje_texto(
                "Tienes una nueva ruta pendiente por completar. "
                "Responde en esta conversacion cuando quieras continuar, pausar o reportar un problema."
            )
            next_stop = result.get("next_route_stop") or {}
            next_clinic = (
                next_stop.get("clinic_name_snapshot")
                or next_stop.get("clinica")
                or "siguiente parada"
            )
            return (
                f"Parada {num_actual}/{total} ({clinic_name}) marcada como completada. "
                f"Siguiente parada pendiente: {next_clinic}. Mensaje enviado al conductor."
            )

        self.context.enviar_mensaje_texto(
            "Por el momento no tienes rutas pendientes.\n"
            "Te notificaremos cuando entre una nueva orden en tu zona."
        )
        return (
            f"Parada {num_actual}/{total} ({clinic_name}) marcada como completada. "
            "No hay mas paradas pendientes."
        )
