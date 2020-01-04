# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2020 Julien Cegarra <julien.cegarra@univ-jfc.fr>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.


""" An independant url widget.
    Most of the new features are in there
"""
import time, datetime
import enum
import attr
import os
from bs4 import BeautifulSoup
from PyQt5.QtCore import (pyqtSignal, pyqtSlot, pyqtProperty, Qt, QSize, QTime,
    QUrl, QTimer)
from PyQt5.QtWidgets import (QApplication, QWidget, QHBoxLayout, QStackedLayout,
    QSizePolicy, QToolBar, QLineEdit, QAction, QStyle, QDialog, QLabel,
    QVBoxLayout, QPushButton, QShortcut)
from PyQt5.QtGui import QIcon, QKeySequence

from qutebrowser.browser import browsertab
from qutebrowser.config import config
from qutebrowser.keyinput import modeman
from qutebrowser.utils import usertypes, log, objreg, utils
from qutebrowser.mainwindow.statusbar import (backforward, command, progress,
                                              keystring, percentage, url,
                                              tabindex)
from qutebrowser.mainwindow.statusbar import text as textwidget

from qutebrowser.utils import urlutils

# Note this has entries for success/error/warn from widgets.webview:LoadStatus
UrlType = enum.Enum('UrlType', ['success', 'success_https', 'error', 'warn',
                                'hover', 'normal'])

TODAY = datetime.date.today().strftime("%d-%m-%Y")

class UrlBar(QWidget):

    STYLESHEET = '''
    QToolBar { padding: 5; }
    QToolBar QToolButton { padding: 5; margin: 2; }
    '''

    resized = pyqtSignal('QRect')
    moved = pyqtSignal('QPoint')


    def add_log(self, logtext):
        """Simple method for logging research data"""
        if not self.START_TIME: # widget not yet started
            return

        m = str((time.time()-self.START_TIME) * 1000.0)
        with open("trace.txt", "a", encoding="utf-8") as ft:
            ft.write("{0};{1}\n".format(m, logtext))

    def __init__(self, *, win_id, private, parent=None):
        super().__init__(parent)
        self.setObjectName(self.__class__.__name__)
        self.setAttribute(Qt.WA_StyledBackground)
        config.set_register_stylesheet(self)

        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._win_id = win_id
        self.cur_tab = None
        self.cmd = command.Command(private=private, win_id=win_id)

        self._urltype = None
        self._hover_url = None
        self._normal_url = None
        self._normal_url_type = UrlType.normal

        self._draw_widgets()
        self._draw_search_box()

        objreg.register('urlbar-widget', self, scope='window',
                        window=win_id)

        QTimer.singleShot(0, self._connect_command_runner)
        self.ignore_first_load = True # hack to prevent repeating data to log
        self.START_TIME = time.time()
        start = time.strftime("%Y-%m-%d %H:%M")
        self.last_html = None
        self.previous_url = None
        self.add_log("START;"+str(start))

    def _connect_command_runner(self):
        window = objreg.get('main-window', scope='window',
                            window=self._win_id)
        self._commandrunner = window._commandrunner

    def _draw_search_box(self):
        """Create layout and add widgets for the 'Ctrl+F' search box """
        screen = QApplication.desktop().screenGeometry(self)

        self.searchbox = QDialog(self)
        self.searchbox.setWindowTitle(" ")
        self.searchbox.resize(400, 100)
        self.searchbox.move(screen.width()-420, 50)

        layouthbox = QHBoxLayout()
        label = QLabel("Rechercher :")

        self.searchedit = QLineEdit("")
        self.searchedit.textChanged.connect(self.on_search)
        layouthbox.addWidget(self.searchedit)

        buttonNext = QPushButton("Suivant")
        layouthbox.addWidget(buttonNext)

        buttonPrevious = QPushButton("Précédent")
        layouthbox.addWidget(buttonPrevious)

        layout = QVBoxLayout()
        layout.addWidget(label)
        layout.addLayout(layouthbox)

        # Set dialog layout
        self.searchbox.setLayout(layout)

        buttonNext.clicked.connect(self.on_search_next)
        buttonPrevious.clicked.connect(self.on_search_previous)

        self.searchbox.setVisible(False)

    def _draw_widgets(self):
        self.navigation_bar = QToolBar("Navigation", self)
        self.navigation_bar.setObjectName("navigation")
        self.navigation_bar.setMovable(False)
        self.navigation_bar.setFloatable(False)

        style = QApplication.instance().style()

        back = QAction(style.standardIcon(QStyle.SP_ArrowLeft), "back", self)
        self.navigation_bar.addAction(back)
        back.triggered.connect(self.on_click_back)

        forward = QAction(style.standardIcon(QStyle.SP_ArrowRight), "forward", self)
        self.navigation_bar.addAction(forward)
        forward.triggered.connect(self.on_click_forward)

        reload = QAction(style.standardIcon(QStyle.SP_BrowserReload), "reload", self)
        self.navigation_bar.addAction(reload)
        reload.triggered.connect(self.on_click_reload)

        search = QShortcut(self)
        search.setContext(Qt.ApplicationShortcut)
        search.setKey(QKeySequence("Ctrl+F"))
        search.activated.connect(self.on_request_search)

        self.url = QLineEdit()

        self.url.setStyleSheet("font-size:18px;")
        self.url.returnPressed.connect(self.on_press_enter)

        self.navigation_bar.addWidget(self.url)


    def on_click_back(self):
        if self.cur_tab.history.can_go_back():
            self._commandrunner.run('back')

    def on_click_forward(self):
        if self.cur_tab.history.can_go_forward():
            self._commandrunner.run('forward')

    def on_click_reload(self):
        self._commandrunner.run('reload')

    def on_press_enter(self):
        """Execute actions when user validated text (url or search items) in the top editbox"""
        url = self.url.text()

        self.add_log("SAISIE_BARREADRESSE;"+url)

        http = "http://"
        https = "https://"

        www = "www."

        if "." in url and (http not in url and https not in url):
            url = http + url

        elif "." not in url:
            url = "http://www.google.com/search?q="+url

        window = objreg.get('tabbed-browser', scope='window',
                            window=self._win_id)
        window.load_url(QUrl(url), False)

    def _set_hbox_padding(self):
        padding = config.val.statusbar.padding
        self._hbox.setContentsMargins(padding.left, 0, padding.right, 0)

    #@pyqtSlot(browsertab.AbstractTab)
    def on_tab_changed(self, tab):
        self.cur_tab = tab
        url = tab.url(requested=True)
        self.on_set_url(url)

    @pyqtSlot()
    def on_load_started(self):
        if self.ignore_first_load:
            self.ignore_first_load = False
        else:
            self.add_log("LOAD STARTED")

    @pyqtSlot()
    def on_load_finished(self):
        self.cur_tab.dump_async(self.dump_html)

    def dump_html(self, html):
        """Backup web pages for future use when the page is loaded [async save]"""
        id = 0;
        while os.path.exists(os.path.join("sauvegarde_pages",TODAY+"-"+str(id)+".html")):
            id+=1

        fp = open(os.path.join("sauvegarde_pages",TODAY+"-"+str(id)+".html"), 'w', encoding="utf-8")
        fp.write(html)
        fp.close()

        self.add_log("LOAD_FINISHED;"+TODAY+"-"+str(id)+".html;"+self.url.text())

        self.getGoogleRanking(self.last_html)

        self.last_html = html


    @pyqtSlot(QUrl)
    def on_set_url(self, url):
        """Setter to be used as a Qt slot.
        Args:
            url: The URL to set as QUrl, or None.
        """
        if url is None:
            self._normal_url = None
        elif not url.isValid():
            self._normal_url = "Invalid URL!"
        else:
            self._normal_url = urlutils.safe_display_string(url)
        self._normal_url_type = UrlType.normal
        self._update_url()


    @pyqtSlot(usertypes.LoadStatus)
    def on_load_status_changed(self, status):
        """Slot for load_status_changed. Sets URL color accordingly.
        Args:
            status: The usertypes.LoadStatus.
        """
        assert isinstance(status, usertypes.LoadStatus), status
        if status in [usertypes.LoadStatus.success,
                      usertypes.LoadStatus.success_https,
                      usertypes.LoadStatus.error,
                      usertypes.LoadStatus.warn]:
            self._normal_url_type = UrlType[status.name]
        else:
            self._normal_url_type = UrlType.normal
        self._update_url()


    def on_request_search(self):
        self.add_log("AFFICHE_BARRERECHERCHE;")
        self.searchbox.setVisible(True)
        self.firstSearch = True

    def on_search(self):
        txt = self.searchedit.text()
        if txt!='':
            self.add_log("LANCERECHERCHE_BARRERECHERCHE;"+txt)
            self._commandrunner.run('search '+txt)

    def on_search_next(self):
        txt = self.searchedit.text()
        if txt!='':
            self.add_log("LANCERECHERCHE_BARRERECHERCHE_SUIVANT;"+txt)
            self._commandrunner.run('search-next')

    def on_search_previous(self):
        txt = self.searchedit.text()
        if txt!='':
            self.add_log("LANCERECHERCHE_BARRERECHERCHE_PRECEDENT;"+txt)
            self._commandrunner.run('search-prev')

    def _update_url(self):
        """Update the displayed URL if the url or the hover url changed."""
        old_urltype = self._urltype

        if self._normal_url is not None:
            self.url.setText(self._normal_url)
            self._urltype = self._normal_url_type
        else:
            self.url.setText('')
            self._urltype = UrlType.normal

        if self.url.text()!=self.previous_url:
            self.previous_url = self.url.text()
            self.add_log("URL_CHANGED;"+self.url.text())

        self.url.setCursorPosition(0)


    def getGoogleRanking(self, html):
        """Add the Google ranking (link and page number) of the last followed link.

           WARNING: might change when Google corp updates the page!
           =======
        """

        if html==None:
            return

        soup = BeautifulSoup(html, "html.parser")

        rank = 1
        found = -1
        for t in soup.findAll("h3", {"class": "LC20lb"}):
            parentdiv = t.find_parent('div')
            if parentdiv:
                links = parentdiv.find_all('a')
                if self.previous_url in links[0].get('href'):
                    found = rank
                rank += 1

        # trouve le numero de page
        footer = soup.find("div", { "id" : "foot" })

        if footer:
            id = footer.find("span", {"class", "csb"})

            page = ""

            for td in footer.find_all('td'):
                id = td.find("span", {"class", "csb"})
                a = td.find("a")
                if id and not a:
                    if td.text!="":
                        page+=td.text


            self.add_log("RANK;"+str(found)+";SUR:;"+str(rank-1)+";PAGE;"+str(page))
        else:
            self.add_log("RANK;"+str(found)+";SUR:;"+str(rank-1)+";PAGE;?")


    @pyqtSlot(str)
    def set_hover_url(self, link):
        """Setter to be used as a Qt slot.
        Saves old shown URL in self._old_url and restores it later if a link is
        "un-hovered" when it gets called with empty parameters.
        Args:
            link: The link which was hovered (string)
        """
        self.add_log("LINK_HOVERED;"+link)
        if link:
            qurl = QUrl(link)
            if qurl.isValid():
                self._hover_url = urlutils.safe_display_string(qurl)
            else:
                self._hover_url = '(invalid URL!) {}'.format(link)
        else:
            self._hover_url = None


    def resizeEvent(self, e):
        """Extend resizeEvent of QWidget to emit a resized signal afterwards.

        Args:
            e: The QResizeEvent.
        """
        super().resizeEvent(e)
        self.resized.emit(self.geometry())

        size = self.minimumSizeHint()
        self.url.resize(size.width()-(64*2)*2, size.height()-4)
        self.navigation_bar.resize(size)


    def moveEvent(self, e):
        """Extend moveEvent of QWidget to emit a moved signal afterwards.

        Args:
            e: The QMoveEvent.
        """
        super().moveEvent(e)
        self.moved.emit(e.pos())

    def minimumSizeHint(self):
        """Set the minimum height to the text height plus some padding."""
        padding = 5
        width = super().width()-2 * padding
        height = self.fontMetrics().height() + padding * 4
        return QSize(width, height)