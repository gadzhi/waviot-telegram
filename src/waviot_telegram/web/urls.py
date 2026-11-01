from django.urls import path
from . import views
urlpatterns = [path('assets/admin.css', views.admin_css, name='admin_css'), path('', views.employees, name='employees'), path('login/', views.login, name='login'), path('logout/', views.logout, name='logout'), path('employees/<int:employee_id>/', views.employee_action, name='employee_action'), path('group/', views.group, name='group'), path('audit/', views.audit, name='audit'), path('health/', views.health)]
