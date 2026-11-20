import sys
from pyftdi.spi import SpiController
from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QLineEdit, QSpinBox, QComboBox, QPlainTextEdit,
    QFileDialog, QLabel, QProgressBar, QMessageBox
)

CMD_READ, CMD_WRITE, CMD_WREN, CMD_RDSR = 0x03, 0x02, 0x06, 0x05


class EepromDriver:
    """Accès bas niveau à une EEPROM SPI 25xx."""
    def __init__(self, url, freq, page_size, addr_bytes):
        self.page_size = page_size
        self.addr_bytes = addr_bytes
        self.spi = SpiController()
        self.spi.configure(url)
        self.slave = self.spi.get_port(cs=0, freq=freq, mode=0)

    def _addr(self, addr):
        return addr.to_bytes(self.addr_bytes, "big")

    def wait_ready(self):
        while self.slave.exchange([CMD_RDSR], 1)[0] & 0x01:
            pass

    def read(self, addr, length, chunk=1024, progress=None):
        out = bytearray()
        while len(out) < length:
            n = min(chunk, length - len(out))
            cmd = bytes([CMD_READ]) + self._addr(addr + len(out))
            out += self.slave.exchange(cmd, n)
            if progress:
                progress(len(out), length)
        return bytes(out)

    def write(self, addr, data, progress=None):
        total, done = len(data), 0
        while done < total:
            cur = addr + done
            n = min(self.page_size - cur % self.page_size, total - done)
            chunk = data[done:done + n]
            self.slave.write([CMD_WREN])
            self.slave.write(bytes([CMD_WRITE]) + self._addr(cur) + chunk)
            self.wait_ready()
            done += n
            if progress:
                progress(done, total)

    def close(self):
        self.spi.terminate()


class Worker(QObject):
    """Exécute les opérations dans un thread séparé."""
    progress = Signal(int, int)
    log = Signal(str)
    finished = Signal()
    data_read = Signal(bytes)

    def __init__(self, params, action, size=0, addr=0, data=b""):
        super().__init__()
        self.params, self.action = params, action
        self.size, self.addr, self.data = size, addr, data

    def run(self):
        drv = None
        try:
            drv = EepromDriver(**self.params)
            if self.action == "read":
                self.log.emit(f"Lecture de {self.size} octets...")
                data = drv.read(self.addr, self.size,
                                progress=self.progress.emit)
                self.data_read.emit(data)
                self.log.emit("Lecture terminée.")
            elif self.action == "write":
                self.log.emit(f"Écriture de {len(self.data)} octets...")
                drv.write(self.addr, self.data, progress=self.progress.emit)
                self.log.emit("Écriture terminée.")
        except Exception as e:
            self.log.emit(f"ERREUR : {e}")
        finally:
            if drv:
                drv.close()
            self.finished.emit()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tigard - Programmateur EEPROM SPI")
        self.resize(700, 550)
        self.last_dump = b""
        self.thread = None

        # --- Paramètres ---
        self.url = QLineEdit("ftdi://ftdi:2232h/2")
        self.freq = QComboBox()
        self.freq.addItems(["100000", "500000", "1000000", "4000000"])
        self.freq.setCurrentText("1000000")
        self.size = QSpinBox()
        self.size.setRange(1, 16 * 1024 * 1024)
        self.size.setValue(32768)  # 25LC256 = 32 Ko
        self.page = QSpinBox()
        self.page.setRange(1, 4096)
        self.page.setValue(64)
        self.addr_bytes = QComboBox()
        self.addr_bytes.addItems(["1", "2", "3"])
        self.addr_bytes.setCurrentText("2")
        self.offset = QSpinBox()
        self.offset.setRange(0, 16 * 1024 * 1024)

        form = QFormLayout()
        form.addRow("URL FTDI :", self.url)
        form.addRow("Fréquence (Hz) :", self.freq)
        form.addRow("Taille (octets) :", self.size)
        form.addRow("Taille de page :", self.page)
        form.addRow("Octets d'adresse :", self.addr_bytes)
        form.addRow("Offset :", self.offset)

        # --- Boutons ---
        self.btn_read = QPushButton("Lire")
        self.btn_save = QPushButton("Enregistrer le dump...")
        self.btn_write = QPushButton("Écrire un fichier...")
        self.btn_save.setEnabled(False)
        btns = QHBoxLayout()
        for b in (self.btn_read, self.btn_save, self.btn_write):
            btns.addWidget(b)

        self.bar = QProgressBar()
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(btns)
        layout.addWidget(self.bar)
        layout.addWidget(QLabel("Journal :"))
        layout.addWidget(self.console)

        self.btn_read.clicked.connect(self.do_read)
        self.btn_save.clicked.connect(self.do_save)
        self.btn_write.clicked.connect(self.do_write)

    def params(self):
        return dict(
            url=self.url.text(),
            freq=float(self.freq.currentText()),
            page_size=self.page.value(),
            addr_bytes=int(self.addr_bytes.currentText()),
        )

    def log(self, msg):
        self.console.appendPlainText(msg)

    def set_busy(self, busy):
        for b in (self.btn_read, self.btn_write):
            b.setEnabled(not busy)

    def start(self, worker):
        self.set_busy(True)
        self.bar.setValue(0)
        self.thread = QThread()
        self.worker = worker
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run)
        worker.progress.connect(
            lambda d, t: self.bar.setValue(int(d * 100 / t)))
        worker.log.connect(self.log)
        worker.data_read.connect(self.on_data)
        worker.finished.connect(self.thread.quit)
        worker.finished.connect(lambda: self.set_busy(False))
        self.thread.start()

    def do_read(self):
        self.start(Worker(self.params(), "read",
                          size=self.size.value(), addr=self.offset.value()))

    def do_write(self):
        path, _ = QFileDialog.getOpenFileName(self, "Fichier à écrire")
        if not path:
            return
        with open(path, "rb") as f:
            data = f.read()
        if len(data) + self.offset.value() > self.size.value():
            QMessageBox.warning(self, "Erreur",
                                "Le fichier dépasse la taille de l'EEPROM.")
            return
        if QMessageBox.question(
                self, "Confirmation",
                f"Écrire {len(data)} octets ?") != QMessageBox.Yes:
            return
        self.start(Worker(self.params(), "write",
                          addr=self.offset.value(), data=data))

    def on_data(self, data):
        self.last_dump = data
        self.btn_save.setEnabled(True)
        self.log(f"{len(data)} octets en mémoire. Aperçu : "
                 f"{data[:16].hex(' ')}")

    def do_save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Enregistrer", "dump.bin")
        if path:
            with open(path, "wb") as f:
                f.write(self.last_dump)
            self.log(f"Dump enregistré dans {path}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
