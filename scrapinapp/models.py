from django.db import models
from django.utils.text import slugify

class Certification(models.Model):
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    description = models.TextField(null=True, blank=True)
    url = models.URLField(max_length=200, null=True, blank=True)
    version = models.CharField(max_length=50, null=True, blank=True)
    regulation_name = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class Clause(models.Model):
    certification = models.ForeignKey(
        Certification, 
        on_delete=models.CASCADE,
        related_name='clauses'
    )
    reference_id = models.CharField(max_length=50)
    display_identifier = models.CharField(max_length=50)
    title = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    original_id = models.CharField(max_length=36, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('certification', 'reference_id')
        ordering = ['reference_id']

    def __str__(self):
        return f"{self.certification.name} - {self.reference_id}: {self.title}"

class Policy(models.Model):
    # Define choices for policy_gathered_from
    POLICY_SOURCE_CHOICES = (
        ('TC', 'TrustCloud'),
        ('ER', 'Eramba'),
    )

    policy_id = models.CharField(max_length=50, unique=True)
    security_group = models.CharField(max_length=50, null=True, blank=True)
    policy_reference = models.CharField(max_length=150, unique=True)
    policy_doc = models.TextField(null=True)
    policy_version = models.TextField(null=True, blank=True)
    title = models.CharField(max_length=255, null=True, blank=True)
    policy_template = models.TextField(null=True, blank=True)
    clauses = models.ManyToManyField(Clause, related_name='policies')
    controls = models.ManyToManyField('Control', related_name='policies', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    policy_gathered_from = models.CharField(
        max_length=2,
        choices=POLICY_SOURCE_CHOICES,
        null=True,
        blank=True
    )

    class Meta:
        verbose_name_plural = "policies"

    def __str__(self):
        return f"{self.policy_id}: {self.title}"

class Control(models.Model):
    CONTROL_SOURCE_CHOICES = (
        ('TC', 'TrustCloud'),
        ('ER', 'Eramba'),
    )
    short_name = models.CharField(max_length=50, unique=True)
    custom_short_name = models.CharField(max_length=50, null=True, blank=True, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField()
    original_id = models.CharField(max_length=36, null=True, blank=True)
    category = models.CharField(max_length=255, null = True, blank=True)
    clauses = models.ManyToManyField(Clause, related_name='controls')
    control_gathered_from = models.CharField(
        max_length=2,
        choices=CONTROL_SOURCE_CHOICES,
        null=True,
        blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.short_name}: {self.name}"
    

# class CheckSync(models.Model):
#     isSyncingInProgress = models.BooleanField(default=False)