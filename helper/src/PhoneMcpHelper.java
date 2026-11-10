import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.content.ClipData;
import android.graphics.Bitmap;
import android.graphics.Point;
import android.graphics.Rect;
import android.os.Build;
import android.os.Bundle;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Base64;
import android.view.Display;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.FileDescriptor;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.List;
import java.util.concurrent.TimeoutException;

/**
 * phone-mcp helper: a small program started with app_process from the ADB shell.
 *
 * It keeps one UiAutomation connection open, so reading the screen takes a fraction of a
 * second instead of the 2-4 s that every `uiautomator dump` needs (JVM start + a fixed
 * 1 s idle wait). It also takes screenshots, injects input and types Unicode text.
 *
 * Protocol: one command per line on stdin, `name {json arguments}`; one reply per command
 * on stdout, a line starting with "@@" followed by a JSON object with "ok".
 * The process exits when stdin closes or after the idle timeout given as the first argument.
 */
public final class PhoneMcpHelper {
    static final int VERSION = 3;
    static final String PREFIX = "@@";
    static final int MAX_NODES = 4000;
    static final int MAX_DEPTH = 80;
    static final int MAX_ATTR = 2000;

    static UiAutomation ua;
    static volatile long lastActivity = SystemClock.uptimeMillis();
    static volatile boolean busy;

    public static void main(String[] args) {
        final long idleMs = args.length > 0 ? Long.parseLong(args[0]) : 120000L;
        final PrintStream out;
        try {
            out = new PrintStream(new FileOutputStream(FileDescriptor.out), false, "UTF-8");
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
        System.setOut(System.err);      // nothing but replies may reach stdout
        Thread.setDefaultUncaughtExceptionHandler(new Thread.UncaughtExceptionHandler() {
            @Override public void uncaughtException(Thread t, Throwable e) {
                e.printStackTrace();
                System.err.flush();
                System.exit(2);
            }
        });
        // The accessibility client posts work to the main looper, so there has to be one,
        // and it has to run: the main thread loops, a worker thread serves the commands.
        Looper.prepareMainLooper();

        Thread watchdog = new Thread(new Runnable() {
            @Override public void run() {
                while (true) {
                    SystemClock.sleep(1000);
                    if (!busy && SystemClock.uptimeMillis() - lastActivity > idleMs) {
                        shutdown();
                        System.exit(0);
                    }
                }
            }
        }, "pmcp-watchdog");
        watchdog.setDaemon(true);
        watchdog.start();

        Thread worker = new Thread(new Runnable() {
            @Override public void run() {
                try {
                    serve(out);
                } catch (Throwable t) {
                    t.printStackTrace();
                }
                shutdown();
                System.exit(0);
            }
        }, "pmcp-worker");
        worker.start();
        Looper.loop();
    }

    static void serve(PrintStream out) throws Exception {
        BufferedReader in = new BufferedReader(new InputStreamReader(new FileInputStream(FileDescriptor.in), "UTF-8"));
        String line;
        while ((line = in.readLine()) != null) {
            line = line.trim();
            if (line.isEmpty()) continue;
            busy = true;
            JSONObject reply;
            try {
                int sp = line.indexOf(' ');
                String cmd = sp < 0 ? line : line.substring(0, sp);
                JSONObject a = sp < 0 ? new JSONObject() : new JSONObject(line.substring(sp + 1));
                if (cmd.equals("quit")) break;
                reply = handle(cmd, a);
            } catch (Throwable t) {
                Throwable c = t;
                while (c instanceof InvocationTargetException && c.getCause() != null) c = c.getCause();
                reply = new JSONObject().put("ok", false).put("err", c.getClass().getSimpleName() + ": " + c.getMessage());
            }
            out.print(PREFIX);
            out.print(reply.toString());
            out.print('\n');
            out.flush();
            lastActivity = SystemClock.uptimeMillis();
            busy = false;
        }
    }

    static void shutdown() {
        try {
            if (ua != null) UiAutomation.class.getMethod("disconnect").invoke(ua);
        } catch (Throwable ignored) {
        }
    }

    static JSONObject ok() throws Exception {
        return new JSONObject().put("ok", true);
    }

    static JSONObject fail(String err) throws Exception {
        return new JSONObject().put("ok", false).put("err", err);
    }

    static JSONObject handle(String cmd, JSONObject a) throws Exception {
        switch (cmd) {
            case "ping":
                return ok().put("v", VERSION).put("sdk", Build.VERSION.SDK_INT);
            case "dump":
                return dump(a);
            case "shot":
                return shot(a);
            case "tap":
                return tap(a);
            case "swipe":
                return swipe(a);
            case "key":
                return key(a);
            case "chars":
                return chars(a);
            case "settext":
                return setText(a);
            case "idle":
                waitIdle(a.optLong("idle", 200), a.optLong("timeout", 2000));
                return ok();
            case "clip_get":
                return clipGet();
            case "clip_set":
                return clipSet(a.getString("s"));
            default:
                return fail("unknown command " + cmd);
        }
    }

    // ---------------------------------------------------------------- connection

    static synchronized UiAutomation ui() throws Exception {
        if (ua != null) return ua;
        HandlerThread thread = new HandlerThread("pmcp-ui");
        thread.start();
        Class<?> connClass = Class.forName("android.app.UiAutomationConnection");
        Class<?> iface = Class.forName("android.app.IUiAutomationConnection");
        Object conn = connClass.getDeclaredConstructor().newInstance();
        UiAutomation u;
        try {
            Constructor<UiAutomation> c = UiAutomation.class.getDeclaredConstructor(Looper.class, iface);
            c.setAccessible(true);
            u = c.newInstance(thread.getLooper(), conn);
        } catch (NoSuchMethodException e) {
            Constructor<UiAutomation> c = UiAutomation.class.getDeclaredConstructor(int.class, Looper.class, iface);
            c.setAccessible(true);
            u = c.newInstance(0, thread.getLooper(), conn);
        }
        try {
            // 1 = FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES: screen readers etc. keep working.
            Method m = UiAutomation.class.getDeclaredMethod("connect", int.class);
            m.setAccessible(true);
            m.invoke(u, 1);
        } catch (NoSuchMethodException e) {
            Method m = UiAutomation.class.getDeclaredMethod("connect");
            m.setAccessible(true);
            m.invoke(u);
        }
        try {
            AccessibilityServiceInfo info = u.getServiceInfo();
            if (info != null) {
                info.flags |= AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                        | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                        | AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
                u.setServiceInfo(info);
            }
        } catch (Throwable ignored) {
        }
        ua = u;
        return u;
    }

    static void waitIdle(long idleMs, long timeoutMs) throws Exception {
        if (idleMs <= 0) return;
        try {
            ui().waitForIdle(idleMs, Math.max(idleMs, timeoutMs));
        } catch (TimeoutException ignored) {
            // The screen keeps changing (animation, video): read it as it is.
        }
    }

    /**
     * Waits until window animations (an app opening, a page sliding in) have finished, so that
     * positions read afterwards are final. The system gives up waiting by itself after 5 s.
     */
    static void waitAnimations() {
        try {
            UiAutomation u = ui();
            try {
                Method m = UiAutomation.class.getMethod("syncInputTransactions", boolean.class);
                m.invoke(u, true);
            } catch (NoSuchMethodException e) {
                UiAutomation.class.getMethod("syncInputTransactions").invoke(u);
            }
        } catch (Throwable ignored) {
            // Not available on this Android version: the caller copes with unfinished animations.
        }
    }

    /** The real size of the screen in its current rotation, or null when it cannot be read. */
    static Point displaySize() {
        try {
            Class<?> dmg = Class.forName("android.hardware.display.DisplayManagerGlobal");
            Object global = dmg.getMethod("getInstance").invoke(null);
            Display d = (Display) dmg.getMethod("getRealDisplay", int.class).invoke(global, Display.DEFAULT_DISPLAY);
            Point p = new Point();
            d.getRealSize(p);
            return p.x > 0 && p.y > 0 ? p : null;
        } catch (Throwable t) {
            return null;
        }
    }

    // ---------------------------------------------------------------- UI tree

    static JSONObject dump(JSONObject a) throws Exception {
        long t0 = SystemClock.uptimeMillis();
        UiAutomation u = ui();
        if (a.optBoolean("anim", true)) waitAnimations();
        waitIdle(a.optLong("idle", 100), a.optLong("timeout", 1500));
        AccessibilityNodeInfo root = null;
        for (int i = 0; i < 10 && root == null; i++) {
            root = u.getRootInActiveWindow();
            if (root == null) SystemClock.sleep(100);
        }
        boolean keyboard = false;
        try {
            List<AccessibilityWindowInfo> windows = u.getWindows();
            for (AccessibilityWindowInfo w : windows) {
                if (w.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD) keyboard = true;
                if (root == null && w.isActive()) root = w.getRoot();
            }
        } catch (Throwable ignored) {
        }
        if (root == null) return fail("no active window");
        Rect screen = new Rect();
        root.getBoundsInScreen(screen);
        StringBuilder sb = new StringBuilder(32768);
        sb.append("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">");
        int[] count = {0};
        dumpNode(root, sb, 0, 0, screen, count);
        sb.append("</hierarchy>");
        JSONObject r = ok().put("xml", sb.toString()).put("keyboard", keyboard).put("nodes", count[0]);
        Point size = displaySize();
        if (size != null) r.put("w", size.x).put("h", size.y);
        return r.put("ms", SystemClock.uptimeMillis() - t0);
    }

    static void dumpNode(AccessibilityNodeInfo n, StringBuilder sb, int index, int depth, Rect screen, int[] count) {
        if (count[0]++ >= MAX_NODES) return;
        Rect b = new Rect();
        n.getBoundsInScreen(b);
        if (!screen.isEmpty() && !b.intersect(screen)) b.setEmpty();
        boolean hint = Build.VERSION.SDK_INT >= 26 && n.isShowingHintText();
        sb.append("<node index=\"").append(index).append('"');
        attr(sb, "text", n.getText());
        attr(sb, "resource-id", n.getViewIdResourceName());
        attr(sb, "class", n.getClassName());
        attr(sb, "package", n.getPackageName());
        attr(sb, "content-desc", n.getContentDescription());
        attr(sb, "checkable", n.isCheckable());
        attr(sb, "checked", n.isChecked());
        attr(sb, "clickable", n.isClickable());
        attr(sb, "enabled", n.isEnabled());
        attr(sb, "focusable", n.isFocusable());
        attr(sb, "focused", n.isFocused());
        attr(sb, "scrollable", n.isScrollable());
        attr(sb, "long-clickable", n.isLongClickable());
        attr(sb, "password", n.isPassword());
        attr(sb, "selected", n.isSelected());
        attr(sb, "editable", n.isEditable());
        if (hint) attr(sb, "hint", true);
        sb.append(" bounds=\"[").append(b.left).append(',').append(b.top).append("][")
                .append(b.right).append(',').append(b.bottom).append("]\"");
        int children = depth < MAX_DEPTH ? n.getChildCount() : 0;
        if (children == 0) {
            sb.append("/>");
            return;
        }
        sb.append('>');
        for (int i = 0; i < children; i++) {
            AccessibilityNodeInfo child = null;
            try {
                child = n.getChild(i);
            } catch (Throwable ignored) {
            }
            if (child == null) continue;
            if (child.isVisibleToUser()) dumpNode(child, sb, i, depth + 1, screen, count);
        }
        sb.append("</node>");
    }

    static void attr(StringBuilder sb, String name, boolean value) {
        sb.append(' ').append(name).append("=\"").append(value).append('"');
    }

    static void attr(StringBuilder sb, String name, CharSequence value) {
        sb.append(' ').append(name).append("=\"");
        if (value != null) {
            int n = Math.min(value.length(), MAX_ATTR);
            for (int i = 0; i < n; i++) {
                char c = value.charAt(i);
                switch (c) {
                    case '&': sb.append("&amp;"); break;
                    case '<': sb.append("&lt;"); break;
                    case '>': sb.append("&gt;"); break;
                    case '"': sb.append("&quot;"); break;
                    case '\n': sb.append("&#10;"); break;
                    case '\t': sb.append(' '); break;
                    default:
                        if (c < 0x20 || c == 0xFFFE || c == 0xFFFF) {
                            sb.append(' ');
                        } else if (Character.isHighSurrogate(c)) {
                            // keep only complete surrogate pairs, XML rejects lone ones
                            if (i + 1 < n && Character.isLowSurrogate(value.charAt(i + 1))) {
                                sb.append(c).append(value.charAt(++i));
                            } else {
                                sb.append(' ');
                            }
                        } else if (Character.isLowSurrogate(c)) {
                            sb.append(' ');
                        } else {
                            sb.append(c);
                        }
                }
            }
        }
        sb.append('"');
    }

    // ---------------------------------------------------------------- screenshot

    static JSONObject shot(JSONObject a) throws Exception {
        long t0 = SystemClock.uptimeMillis();
        if (a.optBoolean("anim", true)) waitAnimations();
        Bitmap bmp = ui().takeScreenshot();
        if (bmp == null) return fail("takeScreenshot returned nothing");
        if (bmp.getConfig() == Bitmap.Config.HARDWARE) bmp = bmp.copy(Bitmap.Config.ARGB_8888, false);
        int w = bmp.getWidth(), h = bmp.getHeight();
        int left = 0, top = 0, cw = w, ch = h;
        JSONArray r = a.optJSONArray("region");
        if (r != null && r.length() == 4) {
            left = Math.max(0, Math.min(w - 1, r.getInt(0)));
            top = Math.max(0, Math.min(h - 1, r.getInt(1)));
            cw = Math.max(1, Math.min(w, r.getInt(2)) - left);
            ch = Math.max(1, Math.min(h, r.getInt(3)) - top);
            bmp = Bitmap.createBitmap(bmp, left, top, cw, ch);
        }
        int maxW = a.optInt("max_w", 720);
        double scale = 1.0;
        if (cw > maxW) {
            scale = (double) maxW / cw;
            bmp = Bitmap.createScaledBitmap(bmp, maxW, Math.max(1, (int) Math.round(ch * scale)), true);
        }
        int ow = bmp.getWidth(), oh = bmp.getHeight();
        int brightest = 0;
        int[] row = new int[ow];
        for (int y = 0; y < oh; y += Math.max(1, oh / 64)) {
            bmp.getPixels(row, 0, ow, 0, y, ow, 1);
            for (int x = 0; x < ow; x += 4) {
                int p = row[x];
                int luma = (((p >> 16) & 0xff) * 3 + ((p >> 8) & 0xff) * 6 + (p & 0xff)) / 10;
                if (luma > brightest) brightest = luma;
            }
        }
        ByteArrayOutputStream buf = new ByteArrayOutputStream(1 << 17);
        bmp.compress(Bitmap.CompressFormat.JPEG, a.optInt("q", 72), buf);
        return ok().put("jpeg", Base64.encodeToString(buf.toByteArray(), Base64.NO_WRAP))
                .put("w", w).put("h", h).put("left", left).put("top", top)
                .put("ow", ow).put("oh", oh).put("scale", scale).put("blank", brightest < 12)
                .put("ms", SystemClock.uptimeMillis() - t0);
    }

    // ---------------------------------------------------------------- input

    static boolean inject(InputEvent e) throws Exception {
        return ui().injectInputEvent(e, true);
    }

    static boolean touch(long down, int action, float x, float y) throws Exception {
        // A finger on the touchscreen, like the `input` command sends. The short form of
        // MotionEvent.obtain() leaves the tool type "unknown", and some apps (the system file
        // picker, for one) ignore such touches.
        MotionEvent.PointerProperties[] props = {new MotionEvent.PointerProperties()};
        props[0].id = 0;
        props[0].toolType = MotionEvent.TOOL_TYPE_FINGER;
        MotionEvent.PointerCoords[] coords = {new MotionEvent.PointerCoords()};
        coords[0].x = x;
        coords[0].y = y;
        coords[0].pressure = 1f;
        coords[0].size = 1f;
        MotionEvent e = MotionEvent.obtain(down, SystemClock.uptimeMillis(), action, 1, props, coords,
                0, 0, 1f, 1f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        try {
            return inject(e);
        } finally {
            e.recycle();
        }
    }

    static JSONObject tap(JSONObject a) throws Exception {
        float x = (float) a.getDouble("x"), y = (float) a.getDouble("y");
        long hold = a.optLong("ms", 0);
        long down = SystemClock.uptimeMillis();
        if (!touch(down, MotionEvent.ACTION_DOWN, x, y)) return fail("input injection was rejected");
        if (hold > 0) SystemClock.sleep(hold);
        touch(down, MotionEvent.ACTION_UP, x, y);
        return ok();
    }

    static JSONObject swipe(JSONObject a) throws Exception {
        float x1 = (float) a.getDouble("x1"), y1 = (float) a.getDouble("y1");
        float x2 = (float) a.getDouble("x2"), y2 = (float) a.getDouble("y2");
        long ms = Math.max(30, a.optLong("ms", 300));
        // hold: keep the finger still at the end before lifting it. The content then stops exactly
        // there instead of flinging on, which makes scrolling by a known distance possible.
        long hold = Math.max(0, a.optLong("hold", 0));
        long down = SystemClock.uptimeMillis();
        if (!touch(down, MotionEvent.ACTION_DOWN, x1, y1)) return fail("input injection was rejected");
        while (true) {
            long elapsed = SystemClock.uptimeMillis() - down;
            if (elapsed >= ms) break;
            float f = (float) elapsed / ms;
            touch(down, MotionEvent.ACTION_MOVE, x1 + (x2 - x1) * f, y1 + (y2 - y1) * f);
            SystemClock.sleep(8);
        }
        touch(down, MotionEvent.ACTION_MOVE, x2, y2);
        long end = SystemClock.uptimeMillis() + hold;
        while (SystemClock.uptimeMillis() < end) {
            SystemClock.sleep(10);
            touch(down, MotionEvent.ACTION_MOVE, x2, y2);
        }
        touch(down, MotionEvent.ACTION_UP, x2, y2);
        return ok();
    }

    static boolean pressKey(int code, int meta) throws Exception {
        long now = SystemClock.uptimeMillis();
        boolean okDown = inject(new KeyEvent(now, now, KeyEvent.ACTION_DOWN, code, 0, meta,
                KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0, InputDevice.SOURCE_KEYBOARD));
        inject(new KeyEvent(now, SystemClock.uptimeMillis(), KeyEvent.ACTION_UP, code, 0, meta,
                KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0, InputDevice.SOURCE_KEYBOARD));
        return okDown;
    }

    static JSONObject key(JSONObject a) throws Exception {
        JSONArray codes = a.getJSONArray("codes");
        int meta = a.optInt("meta", 0);
        for (int i = 0; i < codes.length(); i++) {
            int code = KeyEvent.keyCodeFromString(codes.getString(i));
            if (code == KeyEvent.KEYCODE_UNKNOWN) return fail("unknown key " + codes.getString(i));
            if (!pressKey(code, meta)) return fail("input injection was rejected");
        }
        return ok();
    }

    /** Types text as real key presses. Only characters the virtual keyboard can produce (ASCII). */
    static JSONObject chars(JSONObject a) throws Exception {
        String s = a.getString("s");
        KeyCharacterMap map = KeyCharacterMap.load(KeyCharacterMap.VIRTUAL_KEYBOARD);
        KeyEvent[] events = map.getEvents(s.toCharArray());
        if (events == null) return fail("unmappable characters");
        for (KeyEvent e : events) {
            KeyEvent timed = KeyEvent.changeTimeRepeat(e, SystemClock.uptimeMillis(), 0);
            timed.setSource(InputDevice.SOURCE_KEYBOARD);
            if (!inject(timed)) return fail("input injection was rejected");
        }
        return ok();
    }

    /** Inserts any Unicode text into the focused text field through the accessibility API. */
    static JSONObject setText(JSONObject a) throws Exception {
        String s = a.getString("s");
        boolean replace = a.optBoolean("replace", false);
        AccessibilityNodeInfo f = ui().findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (f == null) return fail("no focused field");
        if (!f.isEditable()) return fail("the focused element is not a text field");
        boolean hint = Build.VERSION.SDK_INT >= 26 && f.isShowingHintText();
        CharSequence cur = f.getText();
        String base = (replace || hint || f.isPassword() || cur == null) ? "" : cur.toString();
        int start = f.getTextSelectionStart(), end = f.getTextSelectionEnd();
        if (start < 0 || end < 0 || start > base.length() || end > base.length()) {
            start = end = base.length();
        }
        int from = Math.min(start, end), to = Math.max(start, end);
        String text = base.substring(0, from) + s + base.substring(to);
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        if (!f.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) return fail("the field rejected the text");
        Bundle sel = new Bundle();
        int cursor = from + s.length();
        sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, cursor);
        sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, cursor);
        f.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, sel);
        return ok();
    }

    // ---------------------------------------------------------------- clipboard

    static Object clipboard() throws Exception {
        IBinder binder = (IBinder) Class.forName("android.os.ServiceManager")
                .getMethod("getService", String.class).invoke(null, "clipboard");
        if (binder == null) throw new IllegalStateException("no clipboard service");
        return Class.forName("android.content.IClipboard$Stub").getMethod("asInterface", IBinder.class)
                .invoke(null, binder);
    }

    /** Calls a clipboard service method whose parameter list differs between Android versions. */
    static Object callClipboard(String name, ClipData clip) throws Exception {
        Object service = clipboard();
        for (Method m : service.getClass().getMethods()) {
            if (!m.getName().equals(name)) continue;
            Class<?>[] types = m.getParameterTypes();
            Object[] args = new Object[types.length];
            int strings = 0;
            for (int i = 0; i < types.length; i++) {
                if (types[i] == ClipData.class) args[i] = clip;
                else if (types[i] == String.class) args[i] = strings++ == 0 ? "com.android.shell" : null;
                else if (types[i] == int.class) args[i] = 0;
                else if (types[i] == boolean.class) args[i] = false;
                else args[i] = null;
            }
            return m.invoke(service, args);
        }
        throw new NoSuchMethodException(name);
    }

    static JSONObject clipGet() throws Exception {
        ClipData clip = (ClipData) callClipboard("getPrimaryClip", null);
        String text = "";
        if (clip != null && clip.getItemCount() > 0) {
            CharSequence t = clip.getItemAt(0).getText();
            if (t != null) text = t.toString();
        }
        return ok().put("text", text);
    }

    static JSONObject clipSet(String s) throws Exception {
        callClipboard("setPrimaryClip", ClipData.newPlainText("phone-mcp", s));
        return ok();
    }
}
