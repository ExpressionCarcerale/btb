# -*- coding: utf-8 -*-
import json

from django.test import TestCase
from django.contrib.auth.models import User, Permission
from django.core.urlresolvers import reverse
from django.conf import settings

from btb.tests import BtbLoginTestCase, BtbMailTestCase
from scanning.models import Document
from comments.models import Comment, CommentRemoval, RemovalReason, Favorite
from correspondence.models import Letter

class TestCommentRemovalMessages(BtbLoginTestCase, BtbMailTestCase):
    def setUp(self):
        super(TestCommentRemovalMessages, self).setUp()

        rr, created = RemovalReason.objects.get_or_create(
                name="Guidelines: personal attacks",
        )
        rr.default_web_message = "This comment was removed for violating the " \
            "<a href='http://betweenthebars.org/about/guidelines/'>guidelines</a>."
        rr.default_comment_author_message = "Your comment was removed for" \
            " violating the site guidelines, found at" \
            " http://betweenthebars.org/about/guidelines/."
        rr.default_post_author_message = "The enclosed comment was removed from" \
            " your blog because we decided that it violates our guidelines," \
            " which forbid personal attacks. We're just letting you know" \
            " because we want you to know what's going on with your blog."
        rr.save()
        self.doc = Document.objects.create(
                author=User.objects.get(username="author"),
                editor=User.objects.get(username="moderator"),
                type="post",
                status="published")

    def test_comment_queryset(self):
        keep = Comment.objects.create(document=self.doc,
                user=User.objects.get(username="reader"), comment="keep")
        remove_without_removal_obj = Comment.objects.create(document=self.doc,
                user=User.objects.get(username="reader"), removed=True,
                comment="remove without removal obj")
        remove_with_tombstone = Comment.objects.create(document=self.doc,
                user=User.objects.get(username="reader"), removed=True,
                comment="remove with tombstone")
        rr = RemovalReason.objects.all()[0]
        CommentRemoval.objects.create(comment=remove_with_tombstone,
                web_message=rr.default_web_message,
                comment_author_message=rr.default_comment_author_message,
                post_author_message=rr.default_post_author_message)
        remove_without_tombstone = Comment.objects.create(document=self.doc,
                user=User.objects.get(username="reader"), removed=True,
                comment="remove without tombstone")
        CommentRemoval.objects.create(comment=remove_without_tombstone,
                web_message="",
                comment_author_message=rr.default_comment_author_message,
                post_author_message=rr.default_post_author_message)

        self.assertEquals(set(Comment.objects.all()), set([
            keep, remove_without_removal_obj, remove_with_tombstone,
            remove_without_tombstone,
        ]))
        self.assertEquals(set(Comment.objects.public()), set([keep]))
        self.assertEquals(set(Comment.objects.public_and_tombstoned()),
                          set([keep, remove_with_tombstone]))
        self.assertEquals(set(Comment.objects.with_mailed_annotation()),
                          set([keep, remove_with_tombstone]))

    def _make_spam(self, username="reader"):
        spam = Comment.objects.create(document=self.doc,
                user=User.objects.get(username=username), comment="spam")
        url = reverse("comments.spam_can_comment", args=[spam.pk])
        return spam, url

    def _make_abuse(self, username="reader"):
        abuse = Comment.objects.create(document=self.doc,
                user=User.objects.get(username=username), comment="abuse")
        url = reverse("comments.moderator_remove", args=[abuse.pk])
        return abuse, url

    def test_spam_can_permissions(self):
        # Must be moderator to spam can.
        (spam, url) = self._make_spam()
        self.loginAs("reader")
        self.assertRedirectsToLogin(url)
        self.loginAs("moderator")
        res = self.client.get(url)
        self.assertEquals(res.status_code, 200)

    def test_spam_can_remove_comment(self):
        self.loginAs("moderator")
        (spam, url) = self._make_spam()
        res = self.client.post(url)
        self.assertEquals(Comment.objects.get(pk=spam.pk).removed, True)
        self.assertFalse(CommentRemoval.objects.filter(comment=spam).exists())
        # No mail notifications for spam.
        self.assertOutboxIsEmpty()

    def test_spam_can_remove_user(self):
        self.loginAs("moderator")
        (spam, url) = self._make_spam()
        res = self.client.post(url, {
            'delete_user': 1
        })
        self.assertFalse(
                Comment.objects.filter(pk=spam.pk).exists())
        self.assertFalse(
                User.objects.filter(username="reader").exists())
        # No mail notifications for spam.
        self.assertOutboxIsEmpty()

    def test_spam_can_unremovable_users(self):
        # We don't want the spam user removal to accidentally remove bloggers,
        # moderators, or superusers.
        cant_remove = ["moderator", "author", "admin"]
        self.loginAs("admin")
        for username in cant_remove:
            (spam, url) = self._make_spam(username)
            res = self.client.post(url, {'delete_user': 1})
            # Comment should exist, and be marked removed.
            self.assertTrue(Comment.objects.get(pk=spam.pk).removed)
            # User should also still exist.
            self.assertTrue(User.objects.filter(username=username).exists())
            # No mail notifications for spam.
            self.assertOutboxIsEmpty()

    def test_remove_no_notice(self):
        (abuse, url) = self._make_abuse("reader")
        self.loginAs("moderator")

        res = self.client.post(url, {
            'comment': abuse.pk,
            'reason': '',
            'web_message': '',
            'comment_author_message': '',
            'post_author_message': '',
        })
        # Success == redirect; error == 200
        self.assertEquals(res.status_code, 302)
        removal = CommentRemoval.objects.get(comment_id=abuse.pk)
        self.assertTrue(Comment.objects.get(pk=abuse.pk).removed)
        self.assertEquals(removal.reason, None)
        self.assertEquals(removal.web_message, "")
        self.assertEquals(removal.comment_author_message, "")
        self.assertEquals(removal.post_author_message, "")
        # No mail.
        self.assertOutboxIsEmpty()

    def test_remove_web_notice(self):
        (abuse, url) = self._make_abuse("reader")
        self.loginAs("moderator")

        res = self.client.post(url, {
            'comment': abuse.pk,
            'reason': '',
            'web_message': 'This is a tombstone...',
            'comment_author_message': '',
            'post_author_message': '',
        })
        self.assertEquals(res.status_code, 302)
        removal = CommentRemoval.objects.get(comment_id=abuse.pk)
        self.assertTrue(Comment.objects.get(pk=abuse.pk).removed)
        self.assertEquals(removal.reason, None)
        self.assertEquals(removal.web_message, "This is a tombstone...")
        self.assertEquals(removal.comment_author_message, "")
        self.assertEquals(removal.post_author_message, "")
        # No mail.
        self.assertOutboxIsEmpty()

        res = self.client.get(abuse.document.get_absolute_url())
        self.assertTrue("This is a tombstone..." in res.content)
        self.assertTrue("[removed]" in res.content)
        # Somehow, res.content is a "str" type here.  Forced to explicitly
        # decode it into utf-8 in order to use "in".
        self.assertFalse(abuse.user.profile.display_name in res.content.decode('utf-8'))

    def test_remove_with_commenter_notice(self):
        (abuse, url) = self._make_abuse("reader")
        # Ensure that reader has an email set.
        u = User.objects.get(username="reader")
        u.email = "reader@example.com"
        u.save()

        self.loginAs("moderator")

        res = self.client.post(url, {
            'comment': abuse.pk,
            'reason': '',
            'web_message': 'This is a tombstone...',
            'comment_author_message': 'You done goofed',
            'post_author_message': '',
        })
        self.assertEquals(res.status_code, 302)
        removal = CommentRemoval.objects.get(comment_id=abuse.pk)
        self.assertTrue(Comment.objects.get(pk=abuse.pk).removed)
        self.assertEquals(removal.reason, None)
        self.assertEquals(removal.web_message, "This is a tombstone...")
        self.assertEquals(removal.comment_author_message, "You done goofed")
        self.assertEquals(removal.post_author_message, "")
        # Mail!
        outbox = self.get_outbox()
        self.assertEquals(len(outbox), 1)
        message = outbox[0]
        body = unicode(message.message())
        self.assertTrue("You done goofed" in body)
        self.assertTrue("abuse" in body)
        self.assertEquals(message.to, [u"reader@example.com"])
        self.assertEquals(message.from_email, settings.DEFAULT_FROM_EMAIL)

        res = self.client.get(abuse.document.get_absolute_url())
        self.assertTrue("This is a tombstone..." in res.content)

        # Even if the CommentRemoval is re-instated, we don't send another
        # notice if it's removed a second time.

        res = self.client.post(reverse("comments.moderator_unremove", args=[abuse.pk]))
        self.clear_outbox()
        self.assertEquals(res.status_code, 302)
        self.assertFalse(CommentRemoval.objects.filter(comment=abuse).exists())
        res = self.client.post(url, {
            'comment': abuse.pk,
            'reason': '',
            'web_message': 'This is a tombstone...',
            'comment_author_message': 'You done goofed',
            'post_author_message': '',
        })
        self.assertEquals(res.status_code, 302)
        self.assertOutboxIsEmpty()

    def test_commentremoval_queryset(self):
        (abuse1, url) = self._make_abuse("reader")
        removal1 = CommentRemoval.objects.create(
                post_author_message="",
                comment=abuse1)
        self.assertEquals(set(CommentRemoval.objects.needing_letters()),
                          set())

        (abuse2, url) = self._make_abuse("reader")
        removal2 = CommentRemoval.objects.create(post_author_message="They done goofed",
            comment=abuse2)
        self.assertEquals(set(CommentRemoval.objects.needing_letters()),
                          set([removal2]))

        self.loginAs("moderator")
        res = self.client.get("/correspondence/needed_letters.json")
        struct = json.loads(res.content)
        self.assertEquals(struct['comment_removal'], 1)

        letter = Letter.objects.create(type="comment_removal",
                sender=User.objects.get(username="moderator"))
            
        letter.comments.add(abuse2)

        self.assertEquals(set(CommentRemoval.objects.needing_letters()),
                          set())

    def test_remove_with_blogger_notice(self):
        (abuse, url) = self._make_abuse("reader")

        self.assertFalse(CommentRemoval.objects.needing_letters().exists())

        self.loginAs("moderator")
        res = self.client.post(url, {
            'comment': abuse.pk,
            'reason': '',
            'web_message': 'This is a tombstone...',
            'comment_author_message': '',
            'post_author_message': 'They done goofed',
        })
        self.assertEquals(res.status_code, 302)
        removal = CommentRemoval.objects.get(comment=abuse)
        self.assertEquals(removal.post_author_message, "They done goofed")
        self.assertTrue(CommentRemoval.objects.needing_letters().exists())

    def test_comments_disabled(self):
        p = self.doc.author.profile
        p.comments_disabled = True
        p.save()
        res = self.client.get(self.doc.get_absolute_url())
        self.assertContains(res, "Comments disabled by author")
        self.assertNotContains(res, "<form")

        self.loginAs("reader")
        res = self.client.post(self.doc.get_absolute_url(), {
            "comment": "This won't work"
        })
        self.assertEquals(res.status_code, 403)

        p.comments_disabled = False
        p.save()
        res = self.client.post(self.doc.get_absolute_url(), {
            "comment": "This will work"
        })
        self.assertEquals(res.status_code, 302)
        self.assertEquals(self.doc.comments.get().comment, "This will work")


class TestFavorites(BtbLoginTestCase):
    def test_login_redirect(self):
        self.assertRedirectsToLogin(reverse("comments.mark_favorite"))
        self.assertRedirectsToLogin(reverse("comments.unmark_favorite"))

    def test_deny_non_public_docs(self):
        doc = Document.objects.create(status="unpublishable",
                author=User.objects.get(username="author"),
                editor=User.objects.get(username="moderator"))

        self.loginAs("reader")
        res = self.client.post(reverse("comments.mark_favorite"),
                {'document_id': doc.pk})
        self.assertEquals(res.status_code, 400)

    def test_require_post(self):
        doc = Document.objects.create(status="published",
                author=User.objects.get(username="author"),
                editor=User.objects.get(username="moderator"))
        self.loginAs("reader")
        res = self.client.get(reverse("comments.mark_favorite"),
                {'document_id': doc.pk})
        self.assertEquals(res.status_code, 400)

    def test_mark_and_unmark_favorite(self):
        doc = Document.objects.create(status="published",
                author=User.objects.get(username="author"),
                editor=User.objects.get(username="moderator"))
        comment = Comment.objects.create(document=doc, 
                user=User.objects.get(username="admin"))
        self.loginAs("reader")
        reader = User.objects.get(username="reader")
        # Add a favorite.

        for pvar in ({'document_id': doc.pk}, {'comment_id': comment.pk}):
            for i in range(2):
                # First time to do it; second time to prove idempotency.
                res = self.client.post(
                        reverse("comments.mark_favorite"), pvar)
                self.assertEquals(res.status_code, 200)
                self.assertTrue("1 Favorite" in res.content)
                self.assertTrue(
                        reverse("comments.unmark_favorite") in res.content)
                self.assertTrue(
                        reverse("comments.mark_favorite") not in res.content)
                self.assertEquals(reader.favorite_set.count(), 1)
            # Remove the favorite.
            for i in range(2):
                # First time to do it; second time to prove idempotency.
                res = self.client.post(
                        reverse("comments.unmark_favorite"), pvar)
                self.assertEquals(res.status_code, 200)
                self.assertTrue(
                        reverse("comments.mark_favorite") in res.content)
                self.assertTrue(
                        reverse("comments.unmark_favorite") not in res.content)
                self.assertEquals(reader.favorite_set.count(), 0)
