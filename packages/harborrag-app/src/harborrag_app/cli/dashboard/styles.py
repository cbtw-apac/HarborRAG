DASHBOARD_CSS = """
Screen {
    background: #07111f;
    color: #dce7f5;
}

Header {
    background: #0d2035;
    color: #dff7ff;
}

Footer {
    background: #0d2035;
    color: #a9c4dc;
}

#dashboard {
    padding: 1 2;
    scrollbar-color: #29b6d8;
    scrollbar-background: #0b1727;
}

.card {
    background: #0b1727;
    border: round #275d8c;
    border-title-color: #65d9ef;
    padding: 0 1;
}

#overview {
    height: 5;
    margin-bottom: 1;
    content-align-vertical: middle;
}

#summary {
    height: 19;
    margin-bottom: 1;
}

#stages {
    width: 2fr;
    height: 100%;
    margin-right: 1;
}

#metrics {
    width: 1fr;
    height: 100%;
}

#progress-title {
    height: 2;
    color: #65d9ef;
    text-style: bold;
    content-align-vertical: middle;
}

#progress {
    height: 3;
    margin: 0 1 1 1;
}

Bar > .bar--bar {
    color: #29b6d8;
    background: #14283b;
}

Bar > .bar--complete {
    color: #4ee0a0;
}

PercentageStatus {
    color: #dff7ff;
    text-style: bold;
}

#counts {
    height: 1fr;
    padding: 0 1;
}

#attention {
    height: 12;
    min-height: 8;
    margin-bottom: 1;
    background: #0b1727;
    border: round #275d8c;
    border-title-color: #65d9ef;
}

#message {
    height: 3;
    margin-bottom: 1;
    color: #8ea9bf;
    content-align: left middle;
}

DataTable > .datatable--header {
    background: #15314d;
    color: #dff7ff;
    text-style: bold;
}

DataTable > .datatable--cursor {
    background: #1d5871;
    color: #ffffff;
}

CancelConfirmation {
    align: center middle;
    background: #020711 70%;
}

#cancel-dialog {
    width: 68;
    height: 12;
    padding: 1 2;
    background: #0b1727;
    border: heavy #e05a67;
}

#cancel-title {
    height: 2;
    color: #ff8b94;
    text-style: bold;
    content-align: center middle;
}

#cancel-copy {
    height: 4;
    content-align: center middle;
}

#cancel-actions {
    height: 3;
    align: center middle;
}

#cancel-actions Button {
    margin: 0 1;
}
"""
