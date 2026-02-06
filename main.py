import sys
import asyncio
import datetime
import re
import logging

from bleak import BleakScanner, BleakClient
from bleak.backends.service import BleakGATTCharacteristic
from bleak.exc import BleakError

import qasync

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem,
    QPushButton, QListWidget, QListWidgetItem,
    QTextEdit, QLabel, QComboBox, QLineEdit,
    QFormLayout, QGroupBox, QCheckBox, QFileDialog
)
from PyQt5.QtGui import QTextCursor

logging.basicConfig(level=logging.INFO)


# ---------- 工具 ----------
def shorten_uuid(uuid, n=8):
    return uuid[:n]


# ---------- 日志 ----------
class BLELogger(QTextEdit):
    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.records = []

    def log(self, msg):
        t = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{t}] {msg}"
        self.records.append(line)
        self.append(line)
        self.moveCursor(QTextCursor.End)

    def clear_log(self):
        self.clear()
        self.records.clear()

    def export(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.records))


# ---------- 扫描 ----------
class BLEDeviceScanner(QWidget):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.scanning = False
        self.scanner = None
        self.devices = {}
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        self.scan_btn = QPushButton("开始扫描")
        self.scan_btn.clicked.connect(self.toggle_scan)
        layout.addWidget(self.scan_btn)

        filter_box = QGroupBox("设备过滤")
        fl = QVBoxLayout(filter_box)

        self.prefix_input = QLineEdit()
        self.prefix_input.setPlaceholderText("设备名前缀")
        fl.addWidget(self.prefix_input)

        self.only_prefix_cb = QCheckBox("仅显示匹配前缀")
        fl.addWidget(self.only_prefix_cb)

        self.hide_unknown_cb = QCheckBox("屏蔽未知设备")
        fl.addWidget(self.hide_unknown_cb)

        layout.addWidget(filter_box)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self.connect_selected)
        layout.addWidget(self.list)

        self.conn_btn = QPushButton("连接")
        self.conn_btn.clicked.connect(self.connect_selected)
        layout.addWidget(self.conn_btn)

    def toggle_scan(self):
        if self.scanning:
            self.stop_scan()
        else:
            self.start_scan()

    def start_scan(self):
        self.devices.clear()
        self.list.clear()
        self.scanning = True
        self.scan_btn.setText("停止扫描")
        self.refresh_timer.start(1000)
        self.main.run_async(self.scan())
        self.main.log.log("开始扫描 BLE 设备")

    def stop_scan(self):
        self.scanning = False
        self.scan_btn.setText("开始扫描")
        self.refresh_timer.stop()
        self.main.log.log("已停止扫描")

    async def scan(self):
        async def on_detect(device, adv):
            self.devices[device.address] = device

        self.scanner = BleakScanner(on_detect)
        await self.scanner.start()

        while self.scanning:
            await asyncio.sleep(0.2)

        await self.scanner.stop()

    def refresh(self):
        current = None
        if self.list.currentItem():
            current = self.list.currentItem().data(Qt.UserRole).address

        self.list.clear()

        prefix = self.prefix_input.text().strip()
        only_prefix = self.only_prefix_cb.isChecked()
        hide_unknown = self.hide_unknown_cb.isChecked()

        for d in self.devices.values():
            name = d.name or ""

            if hide_unknown and not name:
                continue
            if only_prefix and prefix and not name.startswith(prefix):
                continue

            item = QListWidgetItem(f"{name or 'Unknown'} [{d.address}]")
            item.setData(Qt.UserRole, d)
            self.list.addItem(item)

            if d.address == current:
                self.list.setCurrentItem(item)

    def connect_selected(self):
        item = self.list.currentItem()
        if not item:
            return

        self.stop_scan()
        dev = item.data(Qt.UserRole)
        self.main.connect_device(dev)


# ---------- 服务 ----------
class ServiceExplorer(QTreeWidget):
    def __init__(self):
        super().__init__()
        self.setHeaderLabels(["名称", "UUID", "属性"])

    def load(self, services):
        self.clear()
        for srv in services:
            srv_item = QTreeWidgetItem([srv.description or "Service", srv.uuid, ""])
            self.addTopLevelItem(srv_item)
            for ch in srv.characteristics:
                ch_item = QTreeWidgetItem([
                    ch.description or "Characteristic",
                    ch.uuid,
                    ",".join(ch.properties)
                ])
                ch_item.setData(0, Qt.UserRole, ch)
                srv_item.addChild(ch_item)
            srv_item.setExpanded(True)


# ---------- 特性控制 ----------
class CharacteristicControl(QWidget):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.ch = None
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        box = QGroupBox("特性")
        form = QFormLayout(box)
        self.uuid = QLabel("-")
        self.props = QLabel("-")
        form.addRow("UUID", self.uuid)
        form.addRow("属性", self.props)
        layout.addWidget(box)

        self.read_btn = QPushButton("读取")
        self.read_btn.clicked.connect(self.read)
        layout.addWidget(self.read_btn)

        self.format = QComboBox()
        self.format.addItems(["TEXT", "HEX"])
        layout.addWidget(self.format)

        self.input = QLineEdit()
        layout.addWidget(self.input)

        self.write_btn = QPushButton("写入")
        self.write_btn.clicked.connect(self.write)
        layout.addWidget(self.write_btn)

        self.notify_btn = QPushButton("订阅通知")
        self.notify_btn.clicked.connect(self.toggle_notify)
        layout.addWidget(self.notify_btn)

        layout.addStretch()
        self.set_char(None)

    def set_char(self, ch):
        self.ch = ch
        if not ch:
            self.uuid.setText("-")
            self.props.setText("-")
            self.read_btn.setEnabled(False)
            self.write_btn.setEnabled(False)
            self.notify_btn.setEnabled(False)
            return

        self.uuid.setText(ch.uuid)
        self.props.setText(",".join(ch.properties))
        self.read_btn.setEnabled("read" in ch.properties)
        self.write_btn.setEnabled("write" in ch.properties or "write-without-response" in ch.properties)
        self.notify_btn.setEnabled("notify" in ch.properties or "indicate" in ch.properties)

    def read(self):
        self.main.run_async(self.main.read_char(self.ch))

    def write(self):
        self.main.run_async(
            self.main.write_char(
                self.ch,
                self.input.text(),
                self.format.currentText() == "HEX"
            )
        )

    def toggle_notify(self):
        self.main.run_async(self.main.toggle_notify(self.ch))


# ---------- 主窗口 ----------
class BLEDebugTool(QMainWindow):
    def __init__(self):
        super().__init__()
        self.client = None
        self.current_device = None
        self.notifying = set()
        self.setWindowTitle("BLE Debug Tool V2026.2.6")
        self.resize(1200, 800)
        self.setup_ui()

    def setup_ui(self):
        cw = QWidget()
        self.setCentralWidget(cw)
        layout = QVBoxLayout(cw)

        top = QSplitter(Qt.Horizontal)
        self.scanner = BLEDeviceScanner(self)
        self.log = BLELogger()
        top.addWidget(self.scanner)
        top.addWidget(self.log)
        layout.addWidget(top)

        btns = QHBoxLayout()
        clear_btn = QPushButton("清除日志")
        clear_btn.clicked.connect(self.log.clear_log)
        export_btn = QPushButton("导出日志")
        export_btn.clicked.connect(self.export_log)
        btns.addWidget(clear_btn)
        btns.addWidget(export_btn)
        layout.addLayout(btns)

        bottom = QSplitter(Qt.Horizontal)
        self.services = ServiceExplorer()
        self.services.itemSelectionChanged.connect(self.select_item)
        self.ctrl = CharacteristicControl(self)
        bottom.addWidget(self.services)
        bottom.addWidget(self.ctrl)
        layout.addWidget(bottom)

    def export_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", "", "*.log")
        if path:
            self.log.export(path)

    def run_async(self, coro):
        asyncio.ensure_future(coro)

    def connect_device(self, dev):
        self.run_async(self._connect(dev))

    async def _connect(self, dev):
        self.log.log(f"尝试连接设备 {dev.name or 'Unknown'} [{dev.address}]")

        if self.client and self.client.is_connected:
            await self.client.disconnect()
            self.log.log(f"已断开设备 {self.current_device}")

        self.current_device = f"{dev.name or 'Unknown'} [{dev.address}]"
        self.client = BleakClient(dev)

        try:
            await self.client.connect()
        except Exception as e:
            self.log.log(f"❌ 连接失败: {e}")
            return

        self.services.load(self.client.services)
        self.notifying.clear()
        self.log.log(f"已连接 {self.current_device}")

    def select_item(self):
        items = self.services.selectedItems()
        if items and isinstance(items[0].data(0, Qt.UserRole), BleakGATTCharacteristic):
            self.ctrl.set_char(items[0].data(0, Qt.UserRole))
        else:
            self.ctrl.set_char(None)

    async def read_char(self, ch):
        val = await self.client.read_gatt_char(ch.uuid)
        self.log.log(f"READ {shorten_uuid(ch.uuid)} {val}")

    async def write_char(self, ch, data, is_hex):
        payload = bytes.fromhex(re.sub(r"[^0-9A-Fa-f]", "", data)) if is_hex else data.encode()
        await self.client.write_gatt_char(ch.uuid, payload)
        self.log.log(f"WRITE {shorten_uuid(ch.uuid)} {payload}")

    async def toggle_notify(self, ch):
        if ch.uuid in self.notifying:
            await self.client.stop_notify(ch.uuid)
            self.notifying.remove(ch.uuid)
            self.log.log(f"通知已关闭 {shorten_uuid(ch.uuid)}")
            return

        if "notify" not in ch.properties and "indicate" not in ch.properties:
            self.log.log("❌ 该特性不支持 notify / indicate")
            return

        async def cb(_, data):
            self.log.log(f"NOTIFY {shorten_uuid(ch.uuid)} {data}")

        try:
            await self.client.start_notify(ch.uuid, cb)
            self.notifying.add(ch.uuid)
            self.log.log(f"通知已开启 {shorten_uuid(ch.uuid)}")
        except BleakError as e:
            self.log.log(f"❌ 订阅通知失败 {shorten_uuid(ch.uuid)}: {e}")
        except Exception as e:
            self.log.log(f"❌ 未知错误（订阅通知）: {e}")


def main():
    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    win = BLEDebugTool()
    win.show()
    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
