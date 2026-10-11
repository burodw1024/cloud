

# Create your models here.
from django.db import models


class PolicyAIPermission(models.Model):

    class Meta:
        managed = False

        permissions = [
            (
                "use_policy_ai",
                "Can use Policy AI"
            ),
            (
                "manage_policy_rag",
                "Can manage Policy RAG"
            ),
        ]
