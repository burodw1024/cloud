from django.conf import settings
from django.db import models

class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )
    home_branch_code = models.CharField(
        max_length=20,
        blank=True,
        default=""
    )
    allowed_branches_csv = models.TextField(
        blank=True,
        default=""
    )

    def __str__(self):
        return f"{self.user.username} | Branch: {self.home_branch_code}"



class SavedPrompt(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="saved_prompts",
    )
    title = models.CharField(max_length=120)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("owner", "title")]
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"{self.title} ({self.owner})"
#adding for upload file 
# chat/models.py
