<?php
/**
 * Plugin Name: ZivoIT Event Connect for Odoo
 * Plugin URI:  https://www.zivoit.com
 * Description: Display Odoo events on WordPress with the [zivo_events] shortcode and refresh the cache instantly through signed Odoo webhooks.
 * Version:     1.0.0
 * Author:      ZivoIT Technologies
 * License:     GPL-2.0-or-later
 * Text Domain: zivoit-event-connect
 */

if (!defined('ABSPATH')) {
    exit;
}

final class Zivo_Event_Connect
{
    const OPT = 'zivo_event_connect';
    const CACHE_GROUP = 'zivo_ec';

    public static function init()
    {
        add_action('admin_menu', [__CLASS__, 'admin_menu']);
        add_action('admin_init', [__CLASS__, 'register_settings']);
        add_action('rest_api_init', [__CLASS__, 'register_routes']);
        add_shortcode('zivo_events', [__CLASS__, 'shortcode_events']);
        add_shortcode('zivo_event', [__CLASS__, 'shortcode_event']);
        add_action('wp_enqueue_scripts', [__CLASS__, 'styles']);
    }

    /* ------------------------------------------------------------ settings */
    public static function opt($key, $default = '')
    {
        $opts = get_option(self::OPT, []);
        return isset($opts[$key]) && $opts[$key] !== '' ? $opts[$key] : $default;
    }

    public static function admin_menu()
    {
        add_options_page('Odoo Event Connect', 'Odoo Event Connect', 'manage_options', 'zivo-event-connect', [__CLASS__, 'settings_page']);
    }

    public static function register_settings()
    {
        register_setting('zivo_event_connect', self::OPT, ['sanitize_callback' => [__CLASS__, 'sanitize']]);
    }

    public static function sanitize($input)
    {
        return [
            'base_url' => esc_url_raw(rtrim($input['base_url'] ?? '', '/')),
            'api_key' => sanitize_text_field($input['api_key'] ?? ''),
            'webhook_secret' => sanitize_text_field($input['webhook_secret'] ?? ''),
            'cache_ttl' => max(0, intval($input['cache_ttl'] ?? 3600)),
        ];
    }

    public static function settings_page()
    {
        $webhook = esc_url(rest_url('zivo/v1/webhook'));
        ?>
        <div class="wrap">
            <h1>ZivoIT Event Connect for Odoo</h1>
            <form method="post" action="options.php">
                <?php settings_fields('zivo_event_connect'); ?>
                <table class="form-table" role="presentation">
                    <tr><th scope="row">Odoo URL</th><td><input class="regular-text" name="<?php echo self::OPT; ?>[base_url]" value="<?php echo esc_attr(self::opt('base_url')); ?>" placeholder="https://erp.example.com"/></td></tr>
                    <tr><th scope="row">API key</th><td><input class="regular-text" type="password" name="<?php echo self::OPT; ?>[api_key]" value="<?php echo esc_attr(self::opt('api_key')); ?>"/></td></tr>
                    <tr><th scope="row">Webhook secret</th><td><input class="regular-text" type="password" name="<?php echo self::OPT; ?>[webhook_secret]" value="<?php echo esc_attr(self::opt('webhook_secret')); ?>"/>
                        <p class="description">In Odoo › Events › Configuration › Webhooks, set the endpoint to <code><?php echo $webhook; ?></code></p></td></tr>
                    <tr><th scope="row">Cache (seconds)</th><td><input type="number" name="<?php echo self::OPT; ?>[cache_ttl]" value="<?php echo esc_attr(self::opt('cache_ttl', 3600)); ?>"/></td></tr>
                </table>
                <?php submit_button(); ?>
            </form>
            <h2>Usage</h2>
            <p><code>[zivo_events limit="6" department="hr" when="upcoming"]</code> &nbsp; <code>[zivo_event id="12"]</code></p>
        </div>
        <?php
    }

    /* ------------------------------------------------------------ API */
    private static function cache_version()
    {
        return intval(get_option('zivo_ec_cache_version', 1));
    }

    public static function flush_cache()
    {
        update_option('zivo_ec_cache_version', self::cache_version() + 1, false);
        do_action('zivo_event_connect_flushed');
    }

    public static function api_get($path, $args = [])
    {
        $base = self::opt('base_url');
        if (!$base || !self::opt('api_key')) {
            return new WP_Error('zivo_config', 'Odoo Event Connect is not configured.');
        }
        $url = add_query_arg(array_filter($args, 'strlen'), $base . '/api/zivo/v1' . $path);
        $key = 'zivo_ec_' . self::cache_version() . '_' . md5($url);
        $cached = get_transient($key);
        if ($cached !== false) {
            return $cached;
        }
        $response = wp_remote_get($url, ['timeout' => 10, 'headers' => ['X-API-Key' => self::opt('api_key')]]);
        if (is_wp_error($response)) {
            return $response;
        }
        if (wp_remote_retrieve_response_code($response) !== 200) {
            return new WP_Error('zivo_http', 'Odoo returned HTTP ' . wp_remote_retrieve_response_code($response));
        }
        $data = json_decode(wp_remote_retrieve_body($response), true);
        set_transient($key, $data, intval(self::opt('cache_ttl', 3600)));
        return $data;
    }

    /* ------------------------------------------------------------ webhook */
    public static function register_routes()
    {
        register_rest_route('zivo/v1', '/webhook', [
            'methods' => 'POST',
            'callback' => [__CLASS__, 'webhook'],
            'permission_callback' => '__return_true',
        ]);
    }

    public static function webhook(WP_REST_Request $request)
    {
        $secret = self::opt('webhook_secret');
        $timestamp = $request->get_header('x_zivo_timestamp');
        $signature = $request->get_header('x_zivo_signature');
        $body = $request->get_body();
        if (!$secret || !$timestamp || !$signature) {
            return new WP_REST_Response(['ok' => false, 'error' => 'missing signature'], 401);
        }
        if (abs(time() - intval($timestamp)) > 300) {
            return new WP_REST_Response(['ok' => false, 'error' => 'stale request'], 401);
        }
        $expected = 'sha256=' . hash_hmac('sha256', $timestamp . '.' . $body, $secret);
        if (!hash_equals($expected, $signature)) {
            return new WP_REST_Response(['ok' => false, 'error' => 'bad signature'], 401);
        }
        $payload = json_decode($body, true);
        self::flush_cache();
        do_action('zivo_event_connect_webhook', $payload['topic'] ?? '', $payload['data'] ?? [], $payload);
        return new WP_REST_Response(['ok' => true, 'topic' => $payload['topic'] ?? null], 200);
    }

    /* ------------------------------------------------------------ rendering */
    public static function styles()
    {
        wp_register_style('zivo-ec', false);
        wp_enqueue_style('zivo-ec');
        wp_add_inline_style('zivo-ec', '.zivo-ec-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:1.25rem}.zivo-ec-card{border:1px solid #e5e7eb;border-radius:10px;padding:1.1rem;background:#fff}.zivo-ec-date{color:#1f6feb;font-weight:600;font-size:.9rem}.zivo-ec-card h3{margin:.35rem 0 .5rem;font-size:1.1rem}.zivo-ec-badge{display:inline-block;background:#eef3fb;color:#0b1f4b;border-radius:99px;padding:.1rem .6rem;font-size:.75rem;margin-right:.25rem}.zivo-ec-btn{display:inline-block;margin-top:.75rem;background:#0b1f4b;color:#fff!important;padding:.45rem .9rem;border-radius:6px;text-decoration:none}');
    }

    private static function render_card($ev)
    {
        $date = !empty($ev['date_begin_local']) ? date_i18n(get_option('date_format') . ' ' . get_option('time_format'), strtotime($ev['date_begin_local'])) : '';
        $html = '<article class="zivo-ec-card">';
        $html .= '<div class="zivo-ec-date">' . esc_html($date) . '</div>';
        $html .= '<h3>' . esc_html($ev['name']) . '</h3>';
        if (!empty($ev['summary'])) {
            $html .= '<p>' . esc_html(wp_trim_words($ev['summary'], 28)) . '</p>';
        }
        foreach ($ev['departments'] ?? [] as $dept) {
            $html .= '<span class="zivo-ec-badge">' . esc_html($dept['name']) . '</span>';
        }
        if (!empty($ev['location']['city'])) {
            $html .= '<span class="zivo-ec-badge">' . esc_html($ev['location']['city']) . '</span>';
        }
        if (!empty($ev['register_url'])) {
            $html .= '<br/><a class="zivo-ec-btn" href="' . esc_url($ev['register_url']) . '">' . esc_html__('Register', 'zivoit-event-connect') . '</a>';
        }
        return $html . '</article>';
    }

    public static function shortcode_events($atts)
    {
        $atts = shortcode_atts(['limit' => 6, 'department' => '', 'tag' => '', 'when' => 'upcoming', 'q' => ''], $atts, 'zivo_events');
        $data = self::api_get('/events', ['limit' => intval($atts['limit']), 'department' => $atts['department'], 'tag' => $atts['tag'], 'when' => $atts['when'], 'q' => $atts['q']]);
        if (is_wp_error($data)) {
            return current_user_can('manage_options') ? '<p>' . esc_html($data->get_error_message()) . '</p>' : '';
        }
        if (empty($data['data'])) {
            return '<p>' . esc_html__('No upcoming events.', 'zivoit-event-connect') . '</p>';
        }
        $html = '<div class="zivo-ec-grid">';
        foreach ($data['data'] as $ev) {
            $html .= self::render_card($ev);
        }
        return $html . '</div>';
    }

    public static function shortcode_event($atts)
    {
        $atts = shortcode_atts(['id' => ''], $atts, 'zivo_event');
        if (!$atts['id']) {
            return '';
        }
        $data = self::api_get('/events/' . rawurlencode($atts['id']));
        if (is_wp_error($data) || empty($data['data'])) {
            return '';
        }
        $ev = $data['data'];
        return self::render_card($ev) . '<div class="zivo-ec-description">' . wp_kses_post($ev['description_html'] ?? '') . '</div>';
    }
}

Zivo_Event_Connect::init();
