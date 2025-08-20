import sys
from PyQt5.QtWidgets import QApplication
from main_window import AddonUpdater


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = AddonUpdater()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
