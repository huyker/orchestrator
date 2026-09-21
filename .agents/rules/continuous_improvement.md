# Autonomous Continuous Improvement & Self-Healing Rule

## Quy Trình Tự Động Hóa (Automation Workflow)
1. **Hoàn thành tác vụ & Push Git**:
   - Sau khi hoàn thành code/sửa lỗi cho bất kỳ task nào, chạy toàn bộ bộ kiểm thử tự động:
     `python -m unittest discover tests`
   - Kiểm tra working tree, commit và push lên remote Git:
     `git add .` -> `git commit -m "..."` -> `git push origin main`
   - Khởi động hoặc restart server Orchestrator daemon (`python -u app.py`) trên cổng cấu hình để hệ thống luôn hoạt động.

2. **Per-Task Logging (Lưu Log Từng Task)**:
   - Mọi tiến trình chạy task (executor, internal review, validation, user gates, rework, blocked) phải được ghi log độc lập theo từng task vào thư mục `.orchestrator-runtime/task-logs/issue_<number>.log`.
   - Khi task bị BLOCKED hoặc gặp sự cố, chi tiết lỗi và output phải được lưu trữ cục bộ để phục vụ phân tích, KHÔNG gửi spam lỗi hạ tầng lên GitHub Issues.

3. **Vòng Lặp Tự Hoàn Thiện (Continuous Self-Healing Loop - 10 Phút / Lần)**:
   - Định kỳ mỗi 10 phút, tự động kiểm tra sức khỏe hệ thống:
     - Tiến trình server Orchestrator daemon có đang chạy ổn định không.
     - Trạng thái các task trong database `.orchestrator-runtime/state.sqlite3` và GitHub repository.
     - Kiểm tra xem có task nào rơi vào trạng thái BLOCKED, bị deadlock lease, mismatch revision, hoặc lỗi model/executor.
   - Nếu phát hiện lỗi:
     - Phân tích mã nguồn và log chi tiết tại backend/code (không inspect HTML/giao diện trừ khi giao diện bị lỗi layout).
     - Xử lý triệt để nguyên nhân gốc rễ.
     - Chạy lại kiểm thử tự động để bảo đảm chất lượng.
     - Restart lại server daemon và push bản fix lên Git.
   - Quá trình này tạo thành vòng lặp tự động liên tục giúp server tự sửa lỗi và ngày càng hoàn thiện.
