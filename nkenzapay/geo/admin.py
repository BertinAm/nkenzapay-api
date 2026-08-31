from django.contrib import admin

from .models import Corridor, Country, Currency

admin.site.register(Currency)
admin.site.register(Country)
admin.site.register(Corridor)
