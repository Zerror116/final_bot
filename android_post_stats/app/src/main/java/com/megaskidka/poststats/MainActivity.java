package com.megaskidka.poststats;

import android.app.Activity;
import android.content.SharedPreferences;
import android.content.pm.ActivityInfo;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static final String PREFS = "post_statistic_settings";
    private static final String KEY_API_URL = "api_url";
    private static final String KEY_API_TOKEN = "api_token";
    private static final String KEY_LAST_FETCH_AT = "last_fetch_at";
    private static final int FIRST_UPDATE_HOUR = 9;
    private static final int LAST_UPDATE_HOUR = 17;
    private static final String PERIOD_TODAY = "today";
    private static final String PERIOD_WEEK = "week";
    private static final int SWIPE_THRESHOLD_DP = 70;
    private static final TimeZone SAMARA_TIME_ZONE = TimeZone.getTimeZone("Europe/Samara");
    private static final String BACKGROUND = "#14120F";
    private static final String CARD_BACKGROUND = "#F6F2E9";
    private static final int[] ACCENT_COLORS = {
        Color.rgb(15, 118, 110),
        Color.rgb(217, 119, 6),
        Color.rgb(190, 18, 60),
        Color.rgb(37, 99, 235),
        Color.rgb(124, 58, 237),
        Color.rgb(22, 163, 74),
        Color.rgb(194, 65, 12),
        Color.rgb(8, 145, 178),
    };

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Runnable hourlyRefresh = new Runnable() {
        @Override
        public void run() {
            fetchStatistics();
            scheduleNextRefresh();
        }
    };

    private SharedPreferences prefs;
    private LinearLayout root;
    private TextView headerView;
    private EditText apiUrlInput;
    private EditText apiTokenInput;
    private StatisticsSnapshot currentSnapshot;
    private String currentPeriod = PERIOD_TODAY;
    private float touchDownX;
    private float touchDownY;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        configureDisplayMode();
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        showListScreen();

        if (hasSettings()) {
            fetchStatistics();
            scheduleNextRefresh();
        } else {
            showSettingsScreen("Укажите подключение");
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        applyImmersiveMode();
        if (hasSettings() && isRefreshDue()) {
            fetchStatistics();
        }
        scheduleNextRefresh();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            applyImmersiveMode();
        }
    }

    @Override
    protected void onPause() {
        handler.removeCallbacks(hourlyRefresh);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        executor.shutdownNow();
        super.onDestroy();
    }

    @Override
    public boolean dispatchTouchEvent(MotionEvent event) {
        if (event.getAction() == MotionEvent.ACTION_DOWN) {
            touchDownX = event.getX();
            touchDownY = event.getY();
        } else if (event.getAction() == MotionEvent.ACTION_UP) {
            handleSwipe(event.getX() - touchDownX, event.getY() - touchDownY);
        }
        return super.dispatchTouchEvent(event);
    }

    private void configureDisplayMode() {
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);
        Window window = getWindow();
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        window.addFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
        applyImmersiveMode();
    }

    private void applyImmersiveMode() {
        getWindow().getDecorView().setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
        );
    }

    private void showListScreen() {
        ScrollView scrollView = new ScrollView(this);
        scrollView.setFillViewport(true);
        scrollView.setBackgroundColor(color(BACKGROUND));

        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER_VERTICAL);
        root.setMinimumHeight(getResources().getDisplayMetrics().heightPixels);
        root.setPadding(dp(14), dp(10), dp(14), dp(14));
        root.setBackgroundColor(color(BACKGROUND));
        root.setOnLongClickListener(view -> {
            showSettingsScreen("");
            return true;
        });

        scrollView.addView(root, new ScrollView.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ));
        setContentView(scrollView);
    }

    private void showSettingsScreen(String message) {
        root.removeAllViews();
        if (!message.isEmpty()) {
            root.addView(messageText(message, false));
        }

        LinearLayout panel = panel();
        root.addView(panel);

        TextView title = text("Подключение", 18, color("#111827"), Typeface.BOLD);
        panel.addView(title);

        apiUrlInput = input("Адрес API");
        apiUrlInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        apiUrlInput.setText(getApiUrl());
        panel.addView(apiUrlInput, fieldParams());

        apiTokenInput = input("Токен");
        apiTokenInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        apiTokenInput.setText(getApiToken());
        panel.addView(apiTokenInput, fieldParams());

        Button saveButton = button("Сохранить");
        saveButton.setOnClickListener(view -> saveSettings());
        panel.addView(saveButton, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            dp(48)
        ));
    }

    private void saveSettings() {
        String apiUrl = apiUrlInput.getText().toString().trim();
        String apiToken = apiTokenInput.getText().toString().trim();
        if (apiUrl.isEmpty() || apiToken.isEmpty()) {
            showSettingsScreen("Нужны адрес API и токен");
            return;
        }

        prefs.edit()
            .putString(KEY_API_URL, apiUrl)
            .putString(KEY_API_TOKEN, apiToken)
            .apply();
        fetchStatistics();
        scheduleNextRefresh();
    }

    private boolean hasSettings() {
        return !getApiUrl().isEmpty() && !getApiToken().isEmpty();
    }

    private String getApiUrl() {
        String stored = prefs.getString(KEY_API_URL, "");
        if (stored != null && !stored.trim().isEmpty()) {
            return stored.trim();
        }
        return BuildConfig.DEFAULT_API_URL.trim();
    }

    private String getApiToken() {
        String stored = prefs.getString(KEY_API_TOKEN, "");
        if (stored != null && !stored.trim().isEmpty()) {
            return stored.trim();
        }
        return BuildConfig.DEFAULT_API_TOKEN.trim();
    }

    private boolean isRefreshDue() {
        long lastFetchAt = prefs.getLong(KEY_LAST_FETCH_AT, 0L);
        return lastFetchAt <= 0L || System.currentTimeMillis() >= nextScheduledUpdateAfter(lastFetchAt);
    }

    private void fetchStatistics() {
        if (!hasSettings()) {
            showSettingsScreen("Укажите подключение");
            return;
        }

        executor.execute(() -> {
            try {
                StatisticsSnapshot snapshot = requestSnapshot();
                handler.post(() -> renderSnapshot(snapshot));
            } catch (Exception exception) {
                handler.post(this::renderFetchFailure);
            }
        });
    }

    private StatisticsSnapshot requestSnapshot() throws Exception {
        URL url = new URL(getApiUrl());
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) url.openConnection();
            connection.setRequestMethod("GET");
            connection.setConnectTimeout(10000);
            connection.setReadTimeout(10000);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Authorization", "Bearer " + getApiToken());

            int statusCode = connection.getResponseCode();
            InputStream stream = statusCode >= 200 && statusCode < 300
                ? connection.getInputStream()
                : connection.getErrorStream();
            String body = readFully(stream);

            if (statusCode < 200 || statusCode >= 300) {
                throw new IllegalStateException("HTTP " + statusCode);
            }

            JSONObject periods = new JSONObject(body).getJSONObject("periods");
            return new StatisticsSnapshot(
                parseRows(periods.getJSONObject(PERIOD_TODAY).getJSONObject("created_posts")),
                parseRows(periods.getJSONObject(PERIOD_WEEK).getJSONObject("created_posts"))
            );
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private List<StatisticRow> parseRows(JSONObject createdPosts) throws Exception {
        JSONArray byUser = createdPosts.getJSONArray("by_user");

        List<StatisticRow> rows = new ArrayList<>();
        for (int index = 0; index < byUser.length(); index++) {
            JSONObject item = byUser.getJSONObject(index);
            rows.add(new StatisticRow(item.getString("name"), item.getInt("count")));
        }
        return rows;
    }

    private String readFully(InputStream stream) throws Exception {
        if (stream == null) {
            return "";
        }

        try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            StringBuilder builder = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                builder.append(line);
            }
            return builder.toString();
        }
    }

    private void renderSnapshot(StatisticsSnapshot snapshot) {
        currentSnapshot = snapshot;
        prefs.edit().putLong(KEY_LAST_FETCH_AT, System.currentTimeMillis()).apply();
        renderCurrentPeriod();
    }

    private void renderCurrentPeriod() {
        apiUrlInput = null;
        apiTokenInput = null;
        root.removeAllViews();
        addHeader();

        List<StatisticRow> rows = currentPeriodRows();

        if (rows.isEmpty()) {
            root.addView(messageText("Нет данных", false));
            return;
        }

        int columnCount = getColumnCount(rows.size());
        for (int index = 0; index < rows.size(); index += columnCount) {
            LinearLayout rowLayout = new LinearLayout(this);
            rowLayout.setOrientation(LinearLayout.HORIZONTAL);
            rowLayout.setGravity(Gravity.CENTER);
            LinearLayout.LayoutParams rowParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1f
            );
            rowParams.setMargins(0, 0, 0, index + columnCount < rows.size() ? dp(12) : 0);
            root.addView(rowLayout, rowParams);

            int itemsInRow = Math.min(columnCount, rows.size() - index);
            for (int offset = 0; offset < itemsInRow; offset++) {
                rowLayout.addView(workerCard(rows.get(index + offset), index + offset));
            }
        }
    }

    private void renderError(String message) {
        root.removeAllViews();
        addHeader();
        root.addView(messageText(message, true));
    }

    private void renderFetchFailure() {
        if (currentSnapshot != null) {
            renderCurrentPeriod();
            return;
        }

        renderError("Ошибка загрузки");
    }

    private List<StatisticRow> currentPeriodRows() {
        if (currentSnapshot == null) {
            return new ArrayList<>();
        }
        return PERIOD_WEEK.equals(currentPeriod) ? currentSnapshot.weekRows : currentSnapshot.todayRows;
    }

    private void handleSwipe(float deltaX, float deltaY) {
        if (apiUrlInput != null || currentSnapshot == null) {
            return;
        }

        int threshold = dp(SWIPE_THRESHOLD_DP);
        if (Math.abs(deltaX) < threshold || Math.abs(deltaX) <= Math.abs(deltaY) * 1.3f) {
            return;
        }

        currentPeriod = PERIOD_TODAY.equals(currentPeriod) ? PERIOD_WEEK : PERIOD_TODAY;
        renderCurrentPeriod();
    }

    private void scheduleNextRefresh() {
        handler.removeCallbacks(hourlyRefresh);
        if (!hasSettings()) {
            return;
        }

        long nextUpdateAt = nextScheduledUpdateMillis();
        long delay = Math.max(nextUpdateAt - System.currentTimeMillis(), 1000L);
        handler.postDelayed(hourlyRefresh, delay);
        updateNextUpdateText(nextUpdateAt);
    }

    private void addHeader() {
        headerView = text("", 20, color("#F6F2E9"), Typeface.BOLD);
        headerView.setGravity(Gravity.CENTER);
        headerView.setSingleLine(true);
        headerView.setPadding(0, 0, 0, dp(10));
        root.addView(headerView, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ));
        updateNextUpdateText(nextScheduledUpdateMillis());
    }

    private void updateNextUpdateText(long nextUpdateAt) {
        if (headerView == null) {
            return;
        }
        headerView.setText(periodLabel() + " · следующее обновление: " + formatNextUpdate(nextUpdateAt));
    }

    private String periodLabel() {
        return PERIOD_WEEK.equals(currentPeriod) ? "Неделя" : "Сегодня";
    }

    private long nextScheduledUpdateMillis() {
        return nextScheduledUpdateAfter(System.currentTimeMillis());
    }

    private long nextScheduledUpdateAfter(long baseMillis) {
        Calendar now = Calendar.getInstance(SAMARA_TIME_ZONE);
        now.setTimeInMillis(baseMillis);
        Calendar next = Calendar.getInstance(SAMARA_TIME_ZONE);
        next.setTimeInMillis(baseMillis);
        next.set(Calendar.MINUTE, 0);
        next.set(Calendar.SECOND, 0);
        next.set(Calendar.MILLISECOND, 0);

        int hour = now.get(Calendar.HOUR_OF_DAY);
        if (hour < FIRST_UPDATE_HOUR) {
            next.set(Calendar.HOUR_OF_DAY, FIRST_UPDATE_HOUR);
        } else if (hour >= LAST_UPDATE_HOUR) {
            next.add(Calendar.DAY_OF_YEAR, 1);
            next.set(Calendar.HOUR_OF_DAY, FIRST_UPDATE_HOUR);
        } else {
            next.set(Calendar.HOUR_OF_DAY, hour + 1);
        }

        return next.getTimeInMillis();
    }

    private String formatNextUpdate(long nextUpdateAt) {
        Calendar now = Calendar.getInstance(SAMARA_TIME_ZONE);
        Calendar next = Calendar.getInstance(SAMARA_TIME_ZONE);
        next.setTimeInMillis(nextUpdateAt);
        String time = String.format(Locale.getDefault(), "%02d:00", next.get(Calendar.HOUR_OF_DAY));
        boolean tomorrow = now.get(Calendar.YEAR) != next.get(Calendar.YEAR)
            || now.get(Calendar.DAY_OF_YEAR) != next.get(Calendar.DAY_OF_YEAR);
        return tomorrow ? "завтра в " + time : time;
    }

    private int getColumnCount(int rowCount) {
        if (rowCount <= 0) {
            return 1;
        }
        if (rowCount <= 5) {
            return rowCount;
        }
        if (rowCount <= 8) {
            return 4;
        }
        return 5;
    }

    private LinearLayout workerCard(StatisticRow row, int index) {
        int accent = ACCENT_COLORS[index % ACCENT_COLORS.length];
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.HORIZONTAL);
        layout.setGravity(Gravity.CENTER_VERTICAL);
        layout.setPadding(dp(14), dp(10), dp(14), dp(10));
        layout.setBackground(cardBackground());

        View accentView = new View(this);
        accentView.setBackgroundColor(accent);
        LinearLayout.LayoutParams accentParams = new LinearLayout.LayoutParams(dp(6), ViewGroup.LayoutParams.MATCH_PARENT);
        accentParams.setMargins(0, 0, dp(12), 0);
        layout.addView(accentView, accentParams);

        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setGravity(Gravity.CENTER);
        layout.addView(content, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f));

        TextView count = text(String.valueOf(row.count), 52, accent, Typeface.BOLD);
        count.setGravity(Gravity.CENTER);
        count.setSingleLine(true);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            count.setAutoSizeTextTypeUniformWithConfiguration(
                32,
                52,
                1,
                TypedValue.COMPLEX_UNIT_SP
            );
        }
        content.addView(count, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            0,
            1f
        ));

        TextView name = text(row.name, 18, color("#18130E"), Typeface.BOLD);
        name.setGravity(Gravity.CENTER);
        name.setSingleLine(true);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            name.setAutoSizeTextTypeUniformWithConfiguration(
                12,
                18,
                1,
                TypedValue.COMPLEX_UNIT_SP
            );
        }
        content.addView(name, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
            0,
            ViewGroup.LayoutParams.MATCH_PARENT,
            1f
        );
        params.setMargins(index == 0 ? 0 : dp(6), 0, dp(6), 0);
        layout.setLayoutParams(params);
        return layout;
    }

    private TextView messageText(String message, boolean error) {
        TextView textView = text(message, 24, error ? color("#FCA5A5") : color("#F6F2E9"), Typeface.BOLD);
        textView.setGravity(Gravity.CENTER);
        textView.setPadding(dp(16), dp(40), dp(16), dp(16));
        return textView;
    }

    private LinearLayout panel() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(dp(16), dp(16), dp(16), dp(16));
        layout.setBackgroundResource(R.drawable.panel_background);

        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, 0, 0, dp(10));
        layout.setLayoutParams(params);
        return layout;
    }

    private GradientDrawable cardBackground() {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color(CARD_BACKGROUND));
        drawable.setCornerRadius(dp(8));
        drawable.setStroke(dp(1), Color.argb(70, 255, 255, 255));
        return drawable;
    }

    private TextView text(String value, int sp, int textColor, int style) {
        TextView textView = new TextView(this);
        textView.setText(value);
        textView.setTextSize(sp);
        textView.setTextColor(textColor);
        textView.setTypeface(Typeface.DEFAULT, style);
        textView.setIncludeFontPadding(true);
        return textView;
    }

    private EditText input(String hint) {
        EditText editText = new EditText(this);
        editText.setHint(hint);
        editText.setSingleLine(true);
        editText.setTextSize(15);
        editText.setPadding(0, dp(12), 0, dp(12));
        return editText;
    }

    private Button button(String label) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setTextSize(14);
        return button;
    }

    private LinearLayout.LayoutParams fieldParams() {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, dp(8), 0, dp(12));
        return params;
    }

    private int color(String value) {
        return Color.parseColor(value);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private static final class StatisticRow {
        final String name;
        final int count;

        StatisticRow(String name, int count) {
            this.name = name;
            this.count = count;
        }
    }

    private static final class StatisticsSnapshot {
        final List<StatisticRow> todayRows;
        final List<StatisticRow> weekRows;

        StatisticsSnapshot(List<StatisticRow> todayRows, List<StatisticRow> weekRows) {
            this.todayRows = todayRows;
            this.weekRows = weekRows;
        }
    }
}
