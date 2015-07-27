import os

from flask import request, render_template, current_app
from flask.views import View

from werkzeug.wrappers import Response
from werkzeug.exceptions import NotFound

from dulwich.objects import Blob

from klaus import markup, tarutils
from klaus.utils import parent_directory, subpaths, pygmentize, encode_for_git, \
                        force_unicode, guess_is_binary, guess_is_image


def repo_list():
    """Shows a list of all repos and can be sorted by last update. """
    if 'by-last-update' in request.args:
        sort_key = lambda repo: repo.get_last_updated_at()
        reverse = True
    else:
        sort_key = lambda repo: repo.name
        reverse = False
    repos = sorted(current_app.repos, key=sort_key, reverse=reverse)
    return render_template('repo_list.html', repos=repos)


def robots_txt():
    """Serves the robots.txt file to manage the indexing of the site by search enginges"""
    return current_app.send_static_file('robots.txt')


class BaseRepoView(View):
    """
    Base for all views with a repo context.

    The arguments `repo`, `rev`, `path` (see `dispatch_request`) define the
    repository, branch/commit and directory/file context, respectively --
    that is, they specify what (and in what state) is being displayed in all the
    derived views.

    For example: The 'history' view is the `git log` equivalent, i.e. if `path`
    is "/foo/bar", only commits related to "/foo/bar" are displayed, and if
    `rev` is "master", the history of the "master" branch is displayed.
    """
    def __init__(self, view_name, template_name=None):
        self.view_name = view_name
        self.template_name = template_name
        self.context = {}

    def dispatch_request(self, repo, rev=None, path=''):
        self.make_template_context(repo, rev, path.strip('/'))
        return self.get_response()

    def get_response(self):
        return render_template(self.template_name, **self.context)

    def make_template_context(self, repo, rev, path):
        try:
            repo = current_app.repo_map[repo]
        except KeyError:
            raise NotFound("No such repository %r" % repo)

        if rev is None:
            rev = repo.get_default_branch()
            if rev is None:
                raise NotFound("Empty repository")
        try:
            commit = repo.get_commit(rev)
        except KeyError:
            raise NotFound("No such commit %r" % rev)

        try:
            blob_or_tree = repo.get_blob_or_tree(commit, path)
        except KeyError:
            raise NotFound("File not found")

        self.context = {
            'view': self.view_name,
            'repo': repo,
            'rev': rev,
            'commit': commit,
            'branches': repo.get_branch_names(exclude=rev),
            'tags': repo.get_tag_names(),
            'path': path,
            'blob_or_tree': blob_or_tree,
            'subpaths': list(subpaths(path)) if path else None,
        }


class TreeViewMixin(object):
    """
    Implements the logic required for displaying the current directory in the sidebar
    """
    def make_template_context(self, *args):
        super(TreeViewMixin, self).make_template_context(*args)
        self.context['root_tree'] = self.listdir()

    def listdir(self):
        """
        Returns a list of directories and files in the current path of the
        selected commit
        """
        root_directory = self.get_root_directory()
        return self.context['repo'].listdir(
            self.context['commit'],
            root_directory
        )

    def get_root_directory(self):
        root_directory = self.context['path']
        if isinstance(self.context['blob_or_tree'], Blob):
            # 'path' is a file (not folder) name
            root_directory = parent_directory(root_directory)
        return root_directory


class HistoryView(TreeViewMixin, BaseRepoView):
    """ Show commits of a branch + path, just like `git log`. With pagination. """
    def make_template_context(self, *args):
        super(HistoryView, self).make_template_context(*args)

        try:
            page = int(request.args.get('page'))
        except (TypeError, ValueError):
            page = 0

        self.context['page'] = page

        if page:
            history_length = 30
            skip = (self.context['page']-1) * 30 + 10
            if page > 7:
                self.context['previous_pages'] = [0, 1, 2, None] + list(range(page))[-3:]
            else:
                self.context['previous_pages'] = range(page)
        else:
            history_length = 10
            skip = 0

        history = self.context['repo'].history(
            self.context['rev'],
            self.context['path'],
            history_length + 1,
            skip
        )
        if len(history) == history_length + 1:
            # At least one more commit for next page left
            more_commits = True
            # We don't want show the additional commit on this page
            history.pop()
        else:
            more_commits = False

        self.context.update({
            'history': history,
            'more_commits': more_commits,
        })


class BlobViewMixin(object):
    def make_template_context(self, *args):
        super(BlobViewMixin, self).make_template_context(*args)
        self.context['filename'] = os.path.basename(self.context['path'])


class BlobView(BlobViewMixin, TreeViewMixin, BaseRepoView):
    """ Shows a file rendered using ``pygmentize`` """
    def make_template_context(self, *args):
        super(BlobView, self).make_template_context(*args)

        if not isinstance(self.context['blob_or_tree'], Blob):
            raise NotFound("Not a blob")

        binary = guess_is_binary(self.context['blob_or_tree'])
        too_large = sum(map(len, self.context['blob_or_tree'].chunked)) > 100*1024

        if binary:
            self.context.update({
                'is_markup': False,
                'is_binary': True,
                'is_image': False,
            })
            if guess_is_image(self.context['filename']):
                self.context.update({
                    'is_image': True,
                })
        elif too_large:
            self.context.update({
                'too_large': True,
                'is_markup': False,
                'is_binary': False,
            })
        else:
            render_markup = 'markup' not in request.args
            rendered_code = pygmentize(
                force_unicode(self.context['blob_or_tree'].data),
                self.context['filename'],
                render_markup
            )
            self.context.update({
                'too_large': False,
                'is_markup': markup.can_render(self.context['filename']),
                'render_markup': render_markup,
                'rendered_code': rendered_code,
                'is_binary': False,
            })


class RawView(BlobViewMixin, BaseRepoView):
    """
    Shows a single file in raw for (as if it were a normal filesystem file
    served through a static file server)
    """
    def get_response(self):
        # Explicitly set an empty mimetype. This should work well for most
        # browsers as they do file type recognition anyway.
        # The correct way would be to implement proper file type recognition here.
        return Response(self.context['blob_or_tree'].chunked, mimetype='')


class DownloadView(BaseRepoView):
    """
    Download a repo as a tar.gz file
    """
    def get_response(self):
        tarname = "%s@%s.tar.gz" % (self.context['repo'].name, self.context['rev'])
        headers = {
            'Content-Disposition': "attachment; filename=%s" % tarname,
            'Cache-Control': "no-store",  # Disables browser caching
        }

        tar_stream = tarutils.tar_stream(
            self.context['repo'],
            self.context['blob_or_tree'],
            self.context['commit'].commit_time,
            format="gz"
        )
        return Response(
            tar_stream,
            mimetype="application/x-tgz",
            headers=headers
        )


history = HistoryView.as_view('history', 'history', 'history.html')
commit = BaseRepoView.as_view('commit', 'commit', 'view_commit.html')
blob = BlobView.as_view('blob', 'blob', 'view_blob.html')
raw = RawView.as_view('raw', 'raw')
download = DownloadView.as_view('download', 'download')
