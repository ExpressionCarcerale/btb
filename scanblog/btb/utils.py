import json
import os
import logging
from functools import wraps, update_wrapper

from django.conf import settings
from django.core import paginator
from django.core.exceptions import PermissionDenied, ObjectDoesNotExist
from django.db.models import Q, Manager
from django.db.models.query import QuerySet, EmptyQuerySet
from django.http import HttpResponse, Http404
from django.views.generic import View
from sorl.thumbnail.base import ThumbnailBackend, serialize, EXTENSIONS, tokey

logger = logging.getLogger('django.request')

dthandler = lambda obj: obj.isoformat() if hasattr(obj, "isoformat") else None

class OrgManager(Manager):
    def org_filter(self, *args, **kwargs):
        return self.get_queryset().org_filter(*args, **kwargs)

    def mail_filter(self, *args, **kwargs):
        return self.get_queryset().mail_filter(*args, **kwargs)

    def get_queryset(self):
        return self.model.QuerySet(self.model)

class OrgQuerySet(QuerySet):
    orgs = []
    mod_suffix = "moderators"
    mail_mod_suffix = "outgoing_mail_handled_by__moderators"

    def _build_org_q(self, moderator):
        q = Q()
        if not moderator.is_superuser:
            for org_accessor in self.orgs:
                if org_accessor:
                    mods = "__".join((org_accessor, self.mod_suffix))
                else:
                    # The qs in question is an Organization model
                    mods = self.mod_suffix
                q = q | Q(**{mods: moderator})
        return q

    def org_filter(self, moderator, *args, **kwargs):
        """
        Returns the items from organizations for which the given moderator is a
        moderator.
        """
        q = self._build_org_q(moderator)
        return self.filter(q, *args, **kwargs).distinct()

    def mail_filter(self, moderator, *args, **kwargs):
        """
        Returns the items from organizations for which the given moderator is a
        moderator, as well as those from organizations for which the given
        moderator handles mail.
        """
        # Start with the regular organization filter.
        q = self._build_org_q(moderator)
        # OR with any mail mods.
        if not moderator.is_superuser:
            for org_accessor in self.orgs:
                if org_accessor:
                    mail_mods = "__".join((org_accessor, self.mail_mod_suffix))
                else:
                    mail_mods = self.mail_mod_suffix
                q = q | Q(**{mail_mods: moderator})
        return self.filter(q, *args, **kwargs).distinct()


def org_contains(editor, user_id):
    return editor.is_superuser or editor.organizations_moderated.filter(members__pk=user_id).exists()

def can_edit_profile(editor, profile_user_id):
    """
    Returns true if the editor has permission to edit the profile with given
    user_id, false otherwise.
    """
    return editor.id == int(profile_user_id) or (
        editor.has_perm("profiles.change_profile") and 
        org_contains(editor, profile_user_id)
    )

def can_edit_user(editor, user_id):
    """
    Returns true if the editor has permission to edit the user with the given
    user_id, false otherwise.
    """
    return editor.id == int(user_id) or (
            editor.has_perm("auth.change_user") and
            org_contains(editor, user_id)
    )

def date_to_string(date):
    if not date or isinstance(date, basestring):
        return date
    else:
        return date.isoformat()

class permission_required_or_deny(object):
    def __init__(self, *perms):
        self.perms = perms

    def __call__(self, method):
        perms = self.perms
        @wraps(method)
        def wrapped(self, request, *args, **kwargs):
            for perm in perms:
                if not request.user.has_perm(perm):
                    raise PermissionDenied()
            return method(self, request, *args, **kwargs)
        return wrapped

def args_method_decorator(decorator, *dargs, **dkwargs):
    """
    Converts a function decorator into a method decorator, with arguments.
    Copied from django.utils.decorators.method_decorator to allow arguments.
    """
    # 'func' is a function at the time it is passed to _dec, but will eventually
    # be a method of the class it is defined it.
    def _dec(func):
        def _wrapper(self, *args, **kwargs):
            @decorator(*dargs, **dkwargs)
            def bound_func(*args2, **kwargs2):
                return func(self, *args2, **kwargs2)
            # bound_func has the signature that 'decorator' expects i.e.  no
            # 'self' argument, but it is a closure over self so it can call
            # 'func' correctly.
            return bound_func(*args, **kwargs)
        # In case 'decorator' adds attributes to the function it decorates, we
        # want to copy those. We don't have access to bound_func in this scope,
        # but we can cheat by using it on a dummy function.
        @decorator(*dargs, **dkwargs)
        def dummy(*args, **kwargs):
            pass
        update_wrapper(_wrapper, dummy)
        # Need to preserve any existing attributes of 'func', including the name.
        update_wrapper(_wrapper, func)

        return _wrapper
    update_wrapper(_dec, decorator)
    # Change the name to aid debugging.
    _dec.__name__ = 'method_decorator(%s)' % decorator.__name__
    return _dec

class JSONView(View):
    attr_whitelist = []
    def whitelist_attrs(self, kw):
        for key in kw:
            if key not in self.attr_whitelist:
                logger.error("Invalid kwarg: %s (%s)" % (key, kw))
                raise PermissionDenied

    def json_response(self, struct, response=None, content_type="application/json"):
        response = response or HttpResponse()
        response.content = json.dumps(struct, indent=4, default=dthandler)
        response['Content-type'] = content_type
        return response

    def build_paginator(self, request, objects):
        per_page = int(request.GET.get('per_page', 12))
        p = paginator.Paginator(objects, per_page)
        try:
            page = p.page(int(request.GET.get('page', 1)))
        except (ValueError, paginator.InvalidPage):
            page = p.page(1)
        return p, page

    def paginated_response(self, request, objects, 
            dict_method="to_dict", extra=None):
        p, page = self.build_paginator(request, objects)
        struct = {
            u'pagination': {
                u'count': p.count,
                u'page': page.number,
                u'pages': p.num_pages,
                u'per_page': p.per_page,
            },
            u'results': [getattr(obj, dict_method)() for obj in page.object_list]
        }
        if extra:
            struct.update(extra)
        return self.json_response(struct)

    def get_by_id(self, request, queryset, queryarg="id"):
        try:
            obj = queryset.get(pk=request.GET.get(queryarg))
        except ObjectDoesNotExist:
            raise Http404
        return self.json_response(obj.to_dict())

class SameDirThumbnailBackend(ThumbnailBackend):
    def _get_thumbnail_filename(self, source, geometry_string, options):
        """
        Put thumbnails in a subdirectory parallel to the image file.
        """
        key = tokey(source.key, geometry_string, serialize(options))
        return os.path.join(
            settings.MEDIA_ROOT,
            os.path.dirname(source.name),
            settings.THUMBNAIL_PREFIX,
            "%s.%s" % (key, EXTENSIONS[options['format']])
        )
