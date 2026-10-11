from django import forms
from .models import HRNoticeDocument


class HRNoticeUploadForm(forms.ModelForm):
    class Meta:
        model = HRNoticeDocument
        fields = ["file"]

    def clean_file(self):
        f = self.cleaned_data["file"]
        name = f.name.lower()
        if not name.endswith(".docx"):
            raise forms.ValidationError("Only .docx files are allowed.")
        return f
