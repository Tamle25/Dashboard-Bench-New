"""Chạy backend ở chế độ phát triển (tự nạp lại khi sửa code).

Dùng cái này thay cho `uvicorn app.main:app --reload`.

Lý do: repo bench được clone vào data/sources/ và mỗi lượt chạy tạo thêm một worktree
trong data/worktrees/ — hàng trăm file .py nằm ngay trong thư mục mà uvicorn theo dõi.
Đang tải hoặc đang chạy bench thì uvicorn thấy file .py mới và khởi động lại máy chủ giữa
chừng: lượt đồng bộ hỏng, lượt chạy bị huỷ.

`--reload-dir app` KHÔNG chữa được. uvicorn cố tình gộp mọi thư mục theo dõi nằm bên
trong thư mục hiện hành thành chính thư mục hiện hành (WatchFilesReload.__init__), nên
kết quả vẫn là theo dõi toàn bộ backend/. Chỉ `reload_excludes` với đường dẫn TUYỆT ĐỐI
mới có tác dụng, vì FileFilter so khớp nó với `path.parents` của đường dẫn tuyệt đối.
"""

import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_excludes=[str(settings.data_dir.resolve())],
    )
