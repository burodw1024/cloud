from django.db import transaction
from django.utils import timezone

from workflows.models import (
    WorkflowStepAnswer,
    WorkflowAttachment,
    HumanHandoffRequest,
    AccountOpeningRequest,
)
from workflows.services.validators import (
    validate_text,
    validate_mobile,
    validate_nid,
    validate_choice,
    validate_confirm,
)
from workflows.services.backend_submitter import submit_open_account_to_backend


class WorkflowEngine:
    def __init__(self, workflow):
        self.workflow = workflow
        self.definition = workflow.definition.definition_json
        self.steps = self.definition["steps"]

    def _get_step(self, step_name):
        for step in self.steps:
            if step["step"] == step_name:
                return step
        return None

    def _get_step_by_field(self, field_name):
        for step in self.steps:
            if step.get("field") == field_name:
                return step
        return None

    def _next_step(self, current_step):
        for i, step in enumerate(self.steps):
            if step["step"] == current_step and i + 1 < len(self.steps):
                return self.steps[i + 1]
        return None

    def start(self):
        first = self.steps[0]
        self.workflow.current_step = first["step"]
        self.workflow.last_question = first["question"]
        self.workflow.status = "active"
        self.workflow.save(update_fields=["current_step", "last_question", "status", "updated_at"])
        welcome = self.definition.get("welcome_message", "")
        return f"{welcome}\n\n{first['question']}" if welcome else first["question"]

    def resume(self):
        return (
            "You have an incomplete request.\n"
            f"Let's continue from here:\n{self.workflow.last_question}"
        )

    def cancel(self):
        self.workflow.status = "cancelled"
        self.workflow.completed_at = timezone.now()
        self.workflow.save(update_fields=["status", "completed_at", "updated_at"])
        return "Your current request has been cancelled."

    def request_handoff(self, reason=""):
        self.workflow.status = "handoff"
        self.workflow.handoff_requested = True
        self.workflow.save(update_fields=["status", "handoff_requested", "updated_at"])
        HumanHandoffRequest.objects.create(
            workflow=self.workflow,
            reason=reason or "User requested human handoff",
            status="pending"
        )
        return "Your request has been forwarded to a human agent."

    def change_answer(self, field_name):
        step = self._get_step_by_field(field_name)
        if not step:
            return "Requested field was not found in this workflow."
        self.workflow.current_step = step["step"]
        self.workflow.last_question = f"Please provide updated value.\n{step['question']}"
        self.workflow.status = "active"
        self.workflow.save(update_fields=["current_step", "last_question", "status", "updated_at"])
        return self.workflow.last_question

    def validate(self, step, user_input):
        stype = step["type"]
        if stype == "text":
            return validate_text(user_input)
        if stype == "mobile":
            return validate_mobile(user_input)
        if stype == "nid":
            return validate_nid(user_input)
        if stype == "choice":
            return validate_choice(user_input, step.get("choices", []))
        if stype == "confirm":
            return validate_confirm(user_input)
        if stype == "file":
            return True, user_input
        return False, "Unsupported step type."

    def _save_step_answer(self, step_name, field_name, value):
        WorkflowStepAnswer.objects.update_or_create(
            workflow=self.workflow,
            step_name=step_name,
            defaults={
                "field_name": field_name,
                "answer_value": str(value) if value is not None else "",
                "answer_json": {"value": value},
            }
        )

    def _summary(self):
        data = self.workflow.collected_data or {}
        attachments = {a.file_type: a.file.name for a in self.workflow.attachments.all()}
        return (
            "Please confirm the collected information:\n"
            f"Name: {data.get('full_name', '')}\n"
            f"Mobile: {data.get('mobile', '')}\n"
            f"NID: {data.get('nid', '')}\n"
            f"Address: {data.get('address', '')}\n"
            f"Account Type: {data.get('account_type', '')}\n"
            f"NID Front: {attachments.get('nid_front', data.get('nid_front', 'not uploaded'))}\n"
            f"Photo: {attachments.get('photo', data.get('photo', 'not uploaded'))}\n\n"
            "Reply yes or no."
        )

    def _is_skip_file_step(self, step, user_input):
        if step.get("type") != "file":
            return False
        if not step.get("optional", False):
            return False

        text = (user_input or "").strip().lower()
        skip_words = {"skip", "later", "not now", "n/a", "na", "no file"}
        return text in skip_words

    @transaction.atomic
    def process_text(self, user_input):
        current = self._get_step(self.workflow.current_step)
        if not current:
            return {"ok": False, "reply": "Workflow step not found.", "completed": False}

        if self._is_skip_file_step(current, user_input):
            data = self.workflow.collected_data or {}
            data[current["field"]] = "not uploaded"
            self.workflow.collected_data = data
            self._save_step_answer(current["step"], current["field"], "not uploaded")

            nxt = self._next_step(current["step"])
            if not nxt:
                result = self.submit()
                self.workflow.status = "completed"
                self.workflow.completed_at = timezone.now()
                self.workflow.submitted_to_backend = result.get("ok", False)
                self.workflow.backend_reference = result.get("reference")
                self.workflow.save(update_fields=[
                    "collected_data", "status", "completed_at",
                    "submitted_to_backend", "backend_reference", "updated_at"
                ])
                return {"ok": True, "reply": "Workflow completed.", "completed": True}

            self.workflow.current_step = nxt["step"]
            self.workflow.status = "waiting_upload" if nxt["type"] == "file" else "active"
            self.workflow.last_question = self._summary() if nxt["step"] == "confirm" else nxt["question"]
            self.workflow.save(update_fields=["collected_data", "current_step", "status", "last_question", "updated_at"])

            return {"ok": True, "reply": self.workflow.last_question, "completed": False}

        current = self._get_step(self.workflow.current_step)
        if not current:
            return {"ok": False, "reply": "Workflow step not found.", "completed": False}

        valid, value = self.validate(current, user_input)
        if not valid:
            return {"ok": False, "reply": value, "completed": False}

        if current["type"] == "file":
            return {
                "ok": True,
                "reply": "Please use the upload option for this step, or type 'skip' if allowed.",
                "completed": False,
            }

        if current["type"] == "confirm":
            if value is True:
                result = self.submit()
                self.workflow.status = "completed"
                self.workflow.completed_at = timezone.now()
                self.workflow.submitted_to_backend = result.get("ok", False)
                self.workflow.backend_reference = result.get("reference")
                self.workflow.save(update_fields=[
                    "status", "completed_at", "submitted_to_backend",
                    "backend_reference", "updated_at"
                ])
                reply = "Your account opening request has been submitted successfully."
                if result.get("reference"):
                    reply += f" Reference: {result['reference']}"
                return {"ok": True, "reply": reply, "completed": True}

            self.workflow.status = "cancelled"
            self.workflow.completed_at = timezone.now()
            self.workflow.save(update_fields=["status", "completed_at", "updated_at"])
            return {"ok": True, "reply": "Request cancelled.", "completed": True}

        data = self.workflow.collected_data or {}
        data[current["field"]] = value
        self.workflow.collected_data = data
        self._save_step_answer(current["step"], current["field"], value)

        nxt = self._next_step(current["step"])
        if not nxt:
            result = self.submit()
            self.workflow.status = "completed"
            self.workflow.completed_at = timezone.now()
            self.workflow.submitted_to_backend = result.get("ok", False)
            self.workflow.backend_reference = result.get("reference")
            self.workflow.save(update_fields=[
                "collected_data", "status", "completed_at",
                "submitted_to_backend", "backend_reference", "updated_at"
            ])
            return {"ok": True, "reply": "Workflow completed.", "completed": True}

        self.workflow.current_step = nxt["step"]
        if nxt["type"] == "file":
            self.workflow.status = "waiting_upload"
        else:
            self.workflow.status = "active"

        self.workflow.last_question = self._summary() if nxt["step"] == "confirm" else nxt["question"]
        self.workflow.save(update_fields=["collected_data", "current_step", "status", "last_question", "updated_at"])

        return {"ok": True, "reply": self.workflow.last_question, "completed": False}

    @transaction.atomic
    def process_file(self, uploaded_file, file_type):
        current = self._get_step(self.workflow.current_step)
        if not current or current["type"] != "file":
            return {"ok": False, "reply": "No active upload step found.", "completed": False}

        attachment = WorkflowAttachment.objects.create(
            workflow=self.workflow,
            step_name=current["step"],
            field_name=current["field"],
            file_type=file_type,
            file=uploaded_file,
            original_name=getattr(uploaded_file, "name", None),
        )

        data = self.workflow.collected_data or {}
        data[current["field"]] = attachment.file.name
        self.workflow.collected_data = data
        self._save_step_answer(current["step"], current["field"], attachment.file.name)

        nxt = self._next_step(current["step"])
        if not nxt:
            self.workflow.status = "completed"
            self.workflow.completed_at = timezone.now()
            self.workflow.save(update_fields=["collected_data", "status", "completed_at", "updated_at"])
            return {"ok": True, "reply": "File uploaded and workflow completed.", "completed": True}

        self.workflow.current_step = nxt["step"]
        self.workflow.status = "waiting_upload" if nxt["type"] == "file" else "active"
        self.workflow.last_question = self._summary() if nxt["step"] == "confirm" else nxt["question"]
        self.workflow.save(update_fields=["collected_data", "current_step", "status", "last_question", "updated_at"])

        return {"ok": True, "reply": self.workflow.last_question, "completed": False}

    def submit(self):
        data = self.workflow.collected_data or {}
        attachments = {a.file_type: a.file.name for a in self.workflow.attachments.all()}

        if self.workflow.intent_name == "open_account":
            AccountOpeningRequest.objects.update_or_create(
                workflow=self.workflow,
                defaults={
                    "full_name": data.get("full_name", ""),
                    "mobile": data.get("mobile", ""),
                    "nid": data.get("nid", ""),
                    "address": data.get("address", ""),
                    "account_type": data.get("account_type", ""),
                    "nid_front_path": attachments.get("nid_front") or data.get("nid_front"),
                    "photo_path": attachments.get("photo") or data.get("photo"),
                    "status": "submitted",
                }
            )

            return submit_open_account_to_backend(
                self.workflow,
                {
                    "full_name": data.get("full_name", ""),
                    "mobile": data.get("mobile", ""),
                    "nid": data.get("nid", ""),
                    "address": data.get("address", ""),
                    "account_type": data.get("account_type", ""),
                    "nid_front_path": attachments.get("nid_front") or data.get("nid_front"),
                    "photo_path": attachments.get("photo") or data.get("photo"),
                }
            )

        return {"ok": True, "reference": None}
