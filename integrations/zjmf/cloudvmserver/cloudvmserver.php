<?php

use think\Db;

define('CLOUDVMSERVER_DEBUG', false);

function cloudvmserver_debug($message, $data = null)
{
    if (!CLOUDVMSERVER_DEBUG) return;
    $log = '[CLOUDVMSERVER] ' . $message;
    if ($data !== null) {
        $log .= ' | ' . json_encode($data, JSON_UNESCAPED_UNICODE);
    }
    error_log($log);
}

function cloudvmserver_MetaData()
{
    return [
        'DisplayName' => 'CloudVM(PVE) 对接插件',
        'APIVersion'  => 'beta-2.0.0',
        'HelpDoc'     => 'https://github.com/Lorry-San/CloudVM',
    ];
}

function cloudvmserver_ConfigOptions()
{
    return [
        'storage' => [
            'type'        => 'text',
            'name'        => '存储',
            'description' => '留空使用平台默认值，例如 local-lvm',
            'default'     => '',
            'key'         => 'storage',
        ],
        'network_mode' => [
            'type'        => 'dropdown',
            'name'        => '网络模式',
            'description' => 'public 使用 IP 池，nat 使用平台 NAT',
            'default'     => 'nat',
            'key'         => 'network_mode',
            'options'     => ['nat' => 'NAT', 'public' => '公网桥接'],
        ],
        'ci_user' => [
            'type'        => 'text',
            'name'        => '默认用户名',
            'description' => 'Cloud-Init 用户名，留空为 root',
            'default'     => 'root',
            'key'         => 'ci_user',
        ],
        'traffic_limit_gb' => [
            'type'        => 'text',
            'name'        => '月流量限制(GB)',
            'description' => '留空或 0 表示不设置',
            'default'     => '0',
            'key'         => 'traffic_limit_gb',
        ],
        'traffic_reset_policy' => [
            'type'        => 'dropdown',
            'name'        => '流量重置规则',
            'description' => '按开机日期或每月 1 号重置',
            'default'     => 'provision_day',
            'key'         => 'traffic_reset_policy',
            'options'     => ['provision_day' => '开机日期', 'month_first' => '每月1号'],
        ],
        'vnc_ttl_seconds' => [
            'type'        => 'text',
            'name'        => 'VNC Token 有效期(秒)',
            'description' => '60-900',
            'default'     => '900',
            'key'         => 'vnc_ttl_seconds',
        ],
    ];
}

function cloudvmserver_Config($params)
{
    $host = $params['server_ip'] ?? '';
    $port = $params['port'] ?? '';
    $scheme = (!empty($params['serversecure']) || (string)$port === '443') ? 'https' : 'http';
    $base = $scheme . '://' . $host . ($port !== '' ? ':' . $port : '');
    $token = $params['accesshash'] ?? $params['server_password'] ?? '';

    return [rtrim($base, '/'), $token];
}

function cloudvmserver_ApiRequest($params, $path, $data = null, $method = 'GET')
{
    [$base, $token] = cloudvmserver_Config($params);
    if ($base === '' || $token === '') {
        return ['ok' => false, 'http' => 0, 'msg' => 'API 地址或 Token 未配置', 'data' => null];
    }

    $curl = curl_init();
    $options = [
        CURLOPT_URL            => $base . $path,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 45,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_HTTPHEADER     => [
            'X-API-Token: ' . $token,
            'Content-Type: application/json',
        ],
        CURLOPT_SSL_VERIFYPEER => false,
        CURLOPT_SSL_VERIFYHOST => false,
    ];

    if ($data !== null && in_array($method, ['POST', 'PUT', 'PATCH'], true)) {
        $options[CURLOPT_POSTFIELDS] = json_encode($data, JSON_UNESCAPED_UNICODE);
    }

    curl_setopt_array($curl, $options);
    $raw = curl_exec($curl);
    $errno = curl_errno($curl);
    $error = curl_error($curl);
    $http = curl_getinfo($curl, CURLINFO_HTTP_CODE);
    curl_close($curl);

    cloudvmserver_debug('api', ['method' => $method, 'path' => $path, 'http' => $http, 'errno' => $errno]);

    if ($errno) {
        return ['ok' => false, 'http' => $http, 'msg' => $error, 'data' => null];
    }

    $decoded = json_decode($raw, true);
    if ($decoded === null && $raw !== '' && json_last_error() !== JSON_ERROR_NONE) {
        return ['ok' => false, 'http' => $http, 'msg' => 'API 返回非 JSON: ' . substr($raw, 0, 200), 'data' => null];
    }

    return [
        'ok'   => $http >= 200 && $http < 300,
        'http' => $http,
        'msg'  => is_array($decoded) ? ($decoded['detail'] ?? $decoded['msg'] ?? '') : '',
        'data' => $decoded,
    ];
}

function cloudvmserver_Error($res, $fallback)
{
    if (!is_array($res)) return $fallback;
    if (!empty($res['msg'])) return $res['msg'];
    if (isset($res['data']['detail'])) return is_array($res['data']['detail']) ? json_encode($res['data']['detail'], JSON_UNESCAPED_UNICODE) : $res['data']['detail'];
    return $fallback;
}

function cloudvmserver_Success($msg)
{
    return ['status' => 'success', 'msg' => $msg];
}

function cloudvmserver_Fail($msg)
{
    return ['status' => 'error', 'msg' => $msg];
}

function cloudvmserver_GetName($params)
{
    $domain = $params['domain'] ?? '';
    return is_array($domain) ? (string)$domain[0] : (string)$domain;
}

function cloudvmserver_MetaText($params)
{
    $parts = [];
    foreach (['assignedips', 'notes', 'remark'] as $key) {
        if (!empty($params[$key])) $parts[] = (string)$params[$key];
    }
    return implode("\n", $parts);
}

function cloudvmserver_ParseMeta($text)
{
    $meta = [];
    foreach (preg_split('/\r\n|\r|\n/', (string)$text) as $line) {
        if (preg_match('/^\s*([a-zA-Z0-9_]+)\s*=\s*(.*?)\s*$/', $line, $m)) {
            $meta[$m[1]] = $m[2];
        }
    }
    return $meta;
}

function cloudvmserver_GetVmid($params)
{
    foreach (['vmid', 'customfield_vm_id', 'customfield_vmid'] as $key) {
        if (!empty($params[$key]) && is_numeric($params[$key])) return (int)$params[$key];
    }

    $meta = cloudvmserver_ParseMeta(cloudvmserver_MetaText($params));
    if (!empty($meta['vmid']) && is_numeric($meta['vmid'])) return (int)$meta['vmid'];

    $name = cloudvmserver_GetName($params);
    if ($name === '') return 0;

    $res = cloudvmserver_ApiRequest($params, '/api/v1/vms', null, 'GET');
    if (!$res['ok'] || !is_array($res['data'])) return 0;
    foreach ($res['data'] as $vm) {
        if (($vm['name'] ?? '') === $name && isset($vm['vmid'])) return (int)$vm['vmid'];
    }
    return 0;
}

function cloudvmserver_SaveHost($params, $fields)
{
    if (empty($params['hostid'])) return;
    try {
        Db::name('host')->where('id', $params['hostid'])->update($fields);
    } catch (\Exception $e) {
        cloudvmserver_debug('save host failed', $e->getMessage());
    }
}

function cloudvmserver_GetHost($params)
{
    if (empty($params['hostid'])) return [];
    try {
        $host = Db::name('host')->where('id', $params['hostid'])->find();
        return is_array($host) ? $host : [];
    } catch (\Exception $e) {
        cloudvmserver_debug('get host failed', $e->getMessage());
        return [];
    }
}

function cloudvmserver_BuildMeta($data)
{
    $lines = [];
    foreach ($data as $key => $value) {
        if ($value !== null && $value !== '') {
            $lines[] = $key . '=' . $value;
        }
    }
    return implode("\n", $lines);
}

function cloudvmserver_Option($cfg, $keys, $default = null)
{
    foreach ((array)$keys as $key) {
        if (isset($cfg[$key]) && $cfg[$key] !== '') return $cfg[$key];
    }
    return $default;
}

function cloudvmserver_IntOption($cfg, $keys, $default)
{
    $value = cloudvmserver_Option($cfg, $keys, $default);
    return (int)$value;
}

function cloudvmserver_TrafficResetDay($cfg)
{
    $policy = cloudvmserver_Option($cfg, ['traffic_reset_policy', '流量重置规则'], 'provision_day');
    if ($policy === 'month_first' || $policy === '每月1号' || $policy === '每月 1 号') {
        return 1;
    }
    return max(1, min(28, (int)date('j')));
}

function cloudvmserver_BuildCreateRequest($params)
{
    $cfg = $params['configoptions'] ?? [];
    $name = cloudvmserver_GetName($params);
    $networkMode = cloudvmserver_Option($cfg, ['network_mode', '网络模式'], 'nat');
    $rate = (float)cloudvmserver_Option($cfg, ['rate_mbps', 'bandwidth', '带宽', '端口速率'], 0);
    $vlan = trim((string)($cfg['vlan_tag'] ?? ''));

    $network = [
        'mode'     => $networkMode === 'public' ? 'public' : 'nat',
        'model'    => cloudvmserver_Option($cfg, ['network_model', '网卡模型'], 'virtio'),
        'firewall' => ($cfg['firewall'] ?? 'true') === 'true',
    ];
    if (!empty($cfg['bridge'])) $network['bridge'] = $cfg['bridge'];
    if ($rate > 0) $network['rate'] = $rate;
    if ($vlan !== '') $network['vlan_tag'] = (int)$vlan;

    $password = trim((string)($params['password'] ?? ''));
    if ($password === '') {
        $password = substr(str_shuffle('abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'), 0, 12);
    }
    $ciUser = trim($cfg['ci_user'] ?? '') ?: 'root';
    $trafficLimit = (float)cloudvmserver_Option($cfg, ['traffic_limit_gb', 'traffic', '月流量', '流量'], 0);

    $request = [
        'name'                 => $name,
        'image'                => cloudvmserver_Option($cfg, ['image', 'os', 'system', '系统', '镜像'], 'debian-12'),
        'cores'                => cloudvmserver_IntOption($cfg, ['cpus', 'cpu', 'cores', 'CPU'], 2),
        'memory_mb'            => cloudvmserver_IntOption($cfg, ['memory_mb', 'memory', 'ram', '内存'], 2048),
        'disk_gb'              => cloudvmserver_IntOption($cfg, ['disk_gb', 'disk', '硬盘'], 40),
        'network'              => $network,
        'ci_user'              => $ciUser,
        'ci_password'          => $password,
        'allocate_ip'          => ($cfg['allocate_ip'] ?? 'true') === 'true',
        'owner'                => 'zjmf_user_' . ($params['userid'] ?? '0'),
        'start'                => true,
        'traffic_reset_day'    => cloudvmserver_TrafficResetDay($cfg),
        'traffic_reset_hour'   => 0,
        'traffic_reset_timezone' => 'Asia/Shanghai',
    ];

    if (!empty($cfg['template_vmid']) && is_numeric($cfg['template_vmid'])) {
        $request['template_vmid'] = (int)$cfg['template_vmid'];
    }

    foreach (['storage', 'bridge', 'boot_order', 'nameserver', 'searchdomain', 'ssh_keys', 'ip_config', 'expires_at'] as $key) {
        if (!empty($cfg[$key])) $request[$key] = $cfg[$key];
    }
    if ($trafficLimit > 0) $request['traffic_limit_gb'] = $trafficLimit;

    return $request;
}

function cloudvmserver_TestLink($params)
{
    $res = cloudvmserver_ApiRequest($params, '/api/v1/auth/check', null, 'GET');
    return [
        'status' => 200,
        'data'   => [
            'server_status' => $res['ok'] ? 1 : 0,
            'msg'           => $res['ok'] ? '连接成功' : cloudvmserver_Error($res, '连接失败'),
        ],
    ];
}

function cloudvmserver_CreateAccount($params)
{
    $request = cloudvmserver_BuildCreateRequest($params);
    $res = cloudvmserver_ApiRequest($params, '/api/v1/vms', $request, 'POST');
    if (!$res['ok'] || !is_array($res['data'])) {
        return cloudvmserver_Fail(cloudvmserver_Error($res, '创建失败'));
    }

    $data = $res['data'];
    $vmid = $data['vmid'] ?? null;
    if (!$vmid) return cloudvmserver_Fail('创建成功但 API 未返回 vmid');

    $ip = $data['allocated_ip'] ?? $data['nat_ip'] ?? '';
    $meta = cloudvmserver_BuildMeta([
        'vmid'             => $vmid,
        'network_mode'     => $data['network_mode'] ?? '',
        'allocated_ip'     => $data['allocated_ip'] ?? '',
        'nat_ip'           => $data['nat_ip'] ?? '',
        'ssh_port'         => $data['ssh_port'] ?? '',
        'port_range_start' => $data['port_range_start'] ?? '',
        'port_range_end'   => $data['port_range_end'] ?? '',
    ]);

    cloudvmserver_SaveHost($params, [
        'domainstatus' => 'Active',
        'username'     => $request['ci_user'],
        'password'     => $request['ci_password'],
        'dedicatedip'  => $ip,
        'assignedips'  => $meta,
    ]);

    if (!empty($request['expires_at'])) {
        cloudvmserver_SetExpiration($params, (int)$vmid, $request['expires_at'], ($params['configoptions']['expiration_action'] ?? 'pause'));
    }

    return cloudvmserver_Success('创建成功，VMID: ' . $vmid);
}

function cloudvmserver_GetVm($params, $vmid)
{
    return cloudvmserver_ApiRequest($params, '/api/v1/vms/' . (int)$vmid, null, 'GET');
}

function cloudvmserver_GetIps($params, $vmid)
{
    return cloudvmserver_ApiRequest($params, '/api/v1/vms/' . (int)$vmid . '/ips', null, 'GET');
}

function cloudvmserver_GetCredentials($params, $vmid)
{
    return cloudvmserver_ApiRequest($params, '/api/v1/vms/' . (int)$vmid . '/credentials', null, 'GET');
}

function cloudvmserver_SetExpiration($params, $vmid, $expiresAt, $action = 'pause')
{
    if (empty($expiresAt)) return cloudvmserver_Fail('到期时间为空');
    $res = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . (int)$vmid . '/expiration', [
        'expires_at' => $expiresAt,
        'action'     => $action === 'delete' ? 'delete' : 'pause',
    ], 'POST');
    return $res['ok'] ? cloudvmserver_Success('到期动作已设置') : cloudvmserver_Fail(cloudvmserver_Error($res, '到期动作设置失败'));
}

function cloudvmserver_TerminateAccount($params)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');

    $off = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . $vmid . '/pause', null, 'POST');
    if (!$off['ok']) {
        cloudvmserver_debug('stop before delete failed', $off);
    }

    $res = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . $vmid, null, 'DELETE');
    return $res['ok'] ? cloudvmserver_Success('删除任务已提交') : cloudvmserver_Fail(cloudvmserver_Error($res, '删除失败'));
}

function cloudvmserver_On($params)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');
    $res = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . $vmid . '/resume', null, 'POST');
    return $res['ok'] ? cloudvmserver_Success('开机任务已提交') : cloudvmserver_Fail(cloudvmserver_Error($res, '开机失败'));
}

function cloudvmserver_Off($params)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');
    $res = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . $vmid . '/pause', null, 'POST');
    return $res['ok'] ? cloudvmserver_Success('关机任务已提交') : cloudvmserver_Fail(cloudvmserver_Error($res, '关机失败'));
}

function cloudvmserver_Reboot($params)
{
    $off = cloudvmserver_Off($params);
    if (($off['status'] ?? '') !== 'success') return $off;
    usleep(500000);
    return cloudvmserver_On($params);
}

function cloudvmserver_SuspendAccount($params)
{
    return cloudvmserver_Off($params);
}

function cloudvmserver_UnsuspendAccount($params)
{
    return cloudvmserver_On($params);
}

function cloudvmserver_Status($params)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');
    $res = cloudvmserver_ApiRequest($params, '/api/v1/status?vmid=' . $vmid, null, 'GET');
    if (!$res['ok'] || !is_array($res['data'])) {
        return cloudvmserver_Fail(cloudvmserver_Error($res, '查询状态失败'));
    }

    $raw = strtolower((string)($res['data']['status'] ?? 'unknown'));
    $status = 'unknown';
    $des = '未知';
    if (in_array($raw, ['running', 'active'], true)) {
        $status = 'on';
        $des = '运行中';
    } elseif (in_array($raw, ['stopped', 'paused', 'suspended'], true)) {
        $status = 'off';
        $des = '已停止';
    }

    return ['status' => 'success', 'data' => ['status' => $status, 'des' => $des]];
}

function cloudvmserver_Sync($params)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');
    $res = cloudvmserver_GetVm($params, $vmid);
    if (!$res['ok'] || !is_array($res['data'])) {
        return cloudvmserver_Fail(cloudvmserver_Error($res, '同步失败'));
    }

    $data = $res['data'];
    $update = [];
    $raw = strtolower((string)($data['status'] ?? ''));
    if ($raw === 'running') $update['domainstatus'] = 'Active';
    if (in_array($raw, ['stopped', 'paused', 'suspended'], true)) $update['domainstatus'] = 'Suspended';
    if (!empty($data['nat']['ssh_port'])) {
        $update['assignedips'] = cloudvmserver_BuildMeta([
            'vmid' => $vmid,
            'network_mode' => 'nat',
            'nat_ip' => $data['nat']['address'] ?? '',
            'external_host' => $data['nat']['external_host'] ?? '',
            'ssh_port' => $data['nat']['ssh_port'],
            'port_range_start' => $data['nat']['port_start'] ?? '',
            'port_range_end' => $data['nat']['port_end'] ?? '',
        ]);
    }
    if (!empty($update)) cloudvmserver_SaveHost($params, $update);
    return cloudvmserver_Success('同步成功');
}

function cloudvmserver_ChangePackage($params)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');
    $cfg = $params['configoptions'] ?? [];
    $data = [
        'cores'        => cloudvmserver_IntOption($cfg, ['cpus', 'cpu', 'cores', 'CPU'], 0) ?: null,
        'memory_mb'    => cloudvmserver_IntOption($cfg, ['memory_mb', 'memory', 'ram', '内存'], 0) ?: null,
        'network_rate' => (float)cloudvmserver_Option($cfg, ['rate_mbps', 'bandwidth', '带宽', '端口速率'], 0) ?: null,
        'reboot'       => true,
    ];
    $data = array_filter($data, function ($value) { return $value !== null; });
    $res = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . $vmid . '/config', $data, 'PUT');
    return $res['ok'] ? cloudvmserver_Success('套餐变更任务已提交') : cloudvmserver_Fail(cloudvmserver_Error($res, '套餐变更失败'));
}

function cloudvmserver_Reinstall($params)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');
    if (empty($params['reinstall_os'])) return cloudvmserver_Fail('请选择重装镜像');
    $cfg = $params['configoptions'] ?? [];
    $password = trim((string)($params['password'] ?? ''));
    if ($password === '') {
        $host = cloudvmserver_GetHost($params);
        $password = trim((string)($host['password'] ?? ''));
    }
    $data = [
        'image'      => $params['reinstall_os'],
        'password'   => $password !== '' ? $password : null,
        'ci_user'    => trim($cfg['ci_user'] ?? '') ?: 'root',
        'nameserver' => trim($cfg['nameserver'] ?? '') ?: null,
        'storage'    => trim($cfg['storage'] ?? '') ?: null,
        'slot'       => trim($cfg['reinstall_slot'] ?? '') ?: null,
        'disk_size'  => cloudvmserver_IntOption($cfg, ['disk_gb', 'disk', '硬盘'], 0) ? (cloudvmserver_IntOption($cfg, ['disk_gb', 'disk', '硬盘'], 0) . 'G') : null,
        'start'      => true,
        'free_old'   => ($cfg['reinstall_free_old'] ?? 'true') === 'true',
        'dry_run'    => false,
    ];
    $data = array_filter($data, function ($value) { return $value !== null && $value !== ''; });
    $res = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . $vmid . '/reinstall', $data, 'POST');
    return $res['ok'] ? cloudvmserver_Success('重装任务已提交') : cloudvmserver_Fail(cloudvmserver_Error($res, '重装失败'));
}

function cloudvmserver_CrackPassword($params, $new_pass)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');
    $username = $params['username'] ?? (($params['configoptions']['ci_user'] ?? '') ?: 'root');
    $res = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . $vmid . '/credentials', [
        'username' => $username,
        'password' => $new_pass,
    ], 'PUT');
    if (!$res['ok']) return cloudvmserver_Fail(cloudvmserver_Error($res, '更新控制台凭据失败'));
    cloudvmserver_SaveHost($params, ['password' => $new_pass]);
    return cloudvmserver_Success('控制台凭据已更新');
}

function cloudvmserver_TrafficReset($params)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');
    $cfg = $params['configoptions'] ?? [];
    $quota = (float)cloudvmserver_Option($cfg, ['traffic_limit_gb', 'traffic', '月流量', '流量'], 0);
    $data = [
        'quota_gb'    => $quota > 0 ? $quota : null,
        'reset_day'   => cloudvmserver_TrafficResetDay($cfg),
        'reset_hour'  => 0,
        'timezone'    => 'Asia/Shanghai',
        'reset_usage' => true,
    ];
    $res = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . $vmid . '/traffic', $data, 'PUT');
    return $res['ok'] ? cloudvmserver_Success('流量已重置') : cloudvmserver_Fail(cloudvmserver_Error($res, '流量重置失败'));
}

function cloudvmserver_NetworkDisconnect($params)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');
    $res = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . $vmid . '/network/disconnect', null, 'POST');
    return $res['ok'] ? cloudvmserver_Success('断网任务已提交') : cloudvmserver_Fail(cloudvmserver_Error($res, '断网失败'));
}

function cloudvmserver_NetworkConnect($params)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');
    $res = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . $vmid . '/network/connect', null, 'POST');
    return $res['ok'] ? cloudvmserver_Success('恢复网络任务已提交') : cloudvmserver_Fail(cloudvmserver_Error($res, '恢复网络失败'));
}

function cloudvmserver_SetExpire($params)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');
    $cfg = $params['configoptions'] ?? [];
    return cloudvmserver_SetExpiration($params, $vmid, $cfg['expires_at'] ?? '', $cfg['expiration_action'] ?? 'pause');
}

function cloudvmserver_vnc($params)
{
    $vmid = cloudvmserver_GetVmid($params);
    if (!$vmid) return cloudvmserver_Fail('未找到 VMID');
    $ttl = (int)(($params['configoptions']['vnc_ttl_seconds'] ?? 900));
    $ttl = max(60, min(900, $ttl));
    $res = cloudvmserver_ApiRequest($params, '/api/v1/consoles/token', [
        'vmid' => $vmid,
        'ttl_seconds' => $ttl,
    ], 'POST');
    if ($res['ok'] && !empty($res['data']['console_url'])) {
        return ['status' => 'success', 'url' => $res['data']['console_url']];
    }
    return cloudvmserver_Fail(cloudvmserver_Error($res, 'VNC 连接失败'));
}

function cloudvmserver_AdminButton($params)
{
    if (empty($params['domain'])) return [];
    return [
        'Sync' => '同步状态',
        'TrafficReset' => '重置流量',
        'NetworkDisconnect' => '断开网络',
        'NetworkConnect' => '恢复网络',
        'SetExpire' => '设置到期动作',
    ];
}

function cloudvmserver_ClientArea($params)
{
    return ['info' => ['name' => '云服务器信息']];
}

function cloudvmserver_ClientAreaOutput($params, $key)
{
    if ($key !== 'info') return '';
    $vmid = cloudvmserver_GetVmid($params);
    $status = [];
    $vm = [];
    $ips = [];
    $tasks = [];
    $traffic = [];
    $consoleUrl = '';
    $errorMsg = '';
    $host = cloudvmserver_GetHost($params);
    $username = trim((string)($params['username'] ?? ''));
    if ($username === '') $username = trim((string)($host['username'] ?? ''));
    $password = trim((string)($params['password'] ?? ''));
    if ($password === '') $password = trim((string)($host['password'] ?? ''));
    $dedicatedIp = trim((string)($params['dedicatedip'] ?? ''));
    if ($dedicatedIp === '') $dedicatedIp = trim((string)($host['dedicatedip'] ?? ''));
    $assignedIps = trim((string)($params['assignedips'] ?? ''));
    if ($assignedIps === '') $assignedIps = trim((string)($host['assignedips'] ?? ''));

    if ($vmid) {
        $statusRes = cloudvmserver_ApiRequest($params, '/api/v1/status?vmid=' . $vmid, null, 'GET');
        if ($statusRes['ok'] && is_array($statusRes['data'])) {
            $status = $statusRes['data'];
        } else {
            $errorMsg = cloudvmserver_Error($statusRes, '获取实例状态失败');
        }

        $trafficRes = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . $vmid . '/traffic', null, 'GET');
        if ($trafficRes['ok'] && is_array($trafficRes['data'])) {
            $traffic = $trafficRes['data'];
        }

        $vmRes = cloudvmserver_GetVm($params, $vmid);
        if ($vmRes['ok'] && is_array($vmRes['data'])) {
            $vm = $vmRes['data'];
        }

        if ($username === '' || $password === '') {
            $credentialRes = cloudvmserver_GetCredentials($params, $vmid);
            if ($credentialRes['ok'] && is_array($credentialRes['data'])) {
                if ($username === '') $username = trim((string)($credentialRes['data']['username'] ?? ''));
                if ($password === '') $password = trim((string)($credentialRes['data']['password'] ?? ''));
            }
        }

        $ipsRes = cloudvmserver_GetIps($params, $vmid);
        if ($ipsRes['ok'] && is_array($ipsRes['data'])) {
            $ips = $ipsRes['data'];
        }

        $tasksRes = cloudvmserver_ApiRequest($params, '/api/v1/vms/' . $vmid . '/tasks?limit=10', null, 'GET');
        if ($tasksRes['ok'] && is_array($tasksRes['data'])) {
            $tasks = $tasksRes['data'];
        }

        $vnc = cloudvmserver_vnc($params);
        if (($vnc['status'] ?? '') === 'success') $consoleUrl = $vnc['url'];
    } else {
        $errorMsg = '未找到 VMID';
    }

    return [
        'template' => 'templates/info.html',
        'vars' => [
            'vmid'        => $vmid,
            'name'        => cloudvmserver_GetName($params),
            'server_ip'   => $params['server_ip'] ?? '',
            'username'    => $username,
            'password'    => $password,
            'dedicatedip' => $dedicatedIp,
            'assignedips' => $assignedIps,
            'vm'          => $vm,
            'ips'         => $ips,
            'tasks'       => $tasks,
            'status'      => $status,
            'traffic'     => $traffic,
            'console_url' => $consoleUrl,
            'error_msg'   => $errorMsg,
        ],
    ];
}
