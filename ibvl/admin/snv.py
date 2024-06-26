from django.contrib import admin
from ibvl.models import SNV

from .components.filters import InputFilter

class VariantIdFilter(InputFilter):
    parameter_name = 'variant_id'
    title = 'Variant ID'

    def queryset(self, request, queryset):
        if self.value() is not None:
            return queryset.filter(variant__variant_id__icontains=self.value())
        return queryset

class SNVAdmin(admin.ModelAdmin):


    list_display = ('id', 'variant', 'type')
    list_display_links = ('id', 'variant')
    list_filter = (VariantIdFilter,)

admin.site.register(SNV, SNVAdmin)
