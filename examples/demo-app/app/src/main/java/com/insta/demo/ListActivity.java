package com.insta.demo;

import android.content.Intent;
import android.os.Bundle;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ListView;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

import java.util.ArrayList;
import java.util.Arrays;

public class ListActivity extends AppCompatActivity {

    // "蓝牙耳机 Pro" 固定在首位：过滤后仍是 item_text 首个命中项（benchmark t2 依赖）
    private static final String[] ITEMS = {
            "蓝牙耳机 Pro", "机械键盘 87键", "USB-C 扩展坞", "4K 显示器", "无线充电板",
            "降噪头戴耳机", "电动螺丝刀", "智能体脂秤", "桌面摄像头", "电容笔",
            "移动电源 2万毫安", "NAS 双盘位", "Wi-Fi 6 路由器", "蓝牙音箱", "读卡器",
            "桌面麦克风", "手写板", "手机支架", "HDMI 线 2米", "清洁套装"
    };

    private static final int MAX_QUERY = 10;

    private final ArrayList<String> visible = new ArrayList<>();
    private ArrayAdapter<String> adapter;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_list);

        TextView welcomeText = findViewById(R.id.welcome_text);
        String user = getIntent().getStringExtra("user");
        welcomeText.setText(getString(R.string.welcome, user == null ? "tester" : user));

        visible.addAll(Arrays.asList(ITEMS));
        ListView listView = findViewById(R.id.list_view);
        adapter = new ArrayAdapter<>(this, R.layout.row_item, R.id.item_text, visible);
        listView.setAdapter(adapter);
        listView.setOnItemClickListener((parent, view, position, id) -> {
            Intent intent = new Intent(this, DetailActivity.class);
            intent.putExtra("title", visible.get(position));
            startActivity(intent);
        });

        // 故意 bug（对抗集用，勿修）：长按列表项**什么都不发生**（正确行为：弹出上下文菜单）
        listView.setOnItemLongClickListener((parent, view, position, id) -> true);

        EditText searchBox = findViewById(R.id.search_box);
        searchBox.addTextChangedListener(new TextWatcher() {
            @Override
            public void beforeTextChanged(CharSequence s, int start, int count, int after) {
            }

            @Override
            public void onTextChanged(CharSequence s, int start, int before, int count) {
            }

            @Override
            public void afterTextChanged(Editable s) {
                // 故意 bug（对抗集用，勿修）：超过 10 字符**静默截断**，不给任何提示
                if (s.length() > MAX_QUERY) {
                    searchBox.setText(s.subSequence(0, MAX_QUERY));
                    return;
                }
                filter(s.toString().trim());
            }
        });
        searchBox.setOnEditorActionListener((v, actionId, event) -> {
            // 故意 bug（对抗集用，勿修）：空关键词搜索显示 "undefined"（文案 bug）
            if (searchBox.getText().toString().trim().isEmpty()) {
                visible.clear();
                visible.add("undefined");
                adapter.notifyDataSetChanged();
            }
            return true;
        });

        updateChrome();  // 首屏也要显示计数（否则计数框在搜索前是空的）

        // 故意 bug（对抗集用，勿修）：点「导出报表」**直接崩溃**（主线程未捕获异常）。
        // 正确行为：把导出结果写进 `export_result` 并显示出来（见 v09-defect-free 的修复）。
        // ⚠️ 这是**唯一一个"崩溃类"缺陷**——其余 5 个都是静态断言/交互类。
        // 它的探针判据不是 DSL 断言，而是**失败分类必须是 `app-crash`**
        // （`runner.classify_failure` 靠失败步骤的 logcat 里的 `FATAL EXCEPTION` 判定）。
        Button exportBtn = findViewById(R.id.export_report_btn);
        exportBtn.setOnClickListener(v -> {
            throw new IllegalStateException("导出报表失败：未捕获异常（靶场故意缺陷）");
        });
    }

    private void filter(String query) {
        visible.clear();
        for (String item : ITEMS) {
            if (item.contains(query)) {
                visible.add(item);
            }
        }
        adapter.notifyDataSetChanged();
        updateChrome();
    }

    private void updateChrome() {
        // 故意 bug（对抗集用，勿修）：计数用的是**全量** ITEMS 而不是过滤后的 visible
        // ⇒ 筛选出 2 条时仍显示「共 20 项」
        TextView count = findViewById(R.id.item_count);
        count.setText(getString(R.string.item_count, ITEMS.length));

        // 故意 bug（对抗集用，勿修）：结果为空时**不显示**空状态提示
        TextView empty = findViewById(R.id.empty_hint);
        empty.setVisibility(View.GONE);
    }
}
