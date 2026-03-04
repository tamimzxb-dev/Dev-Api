<?php

declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');

$remoteBase = 'http://54.39.104.241/ints/agent/res/data_smscdr.php';
$query = $_GET;
$remoteUrl = $remoteBase;
if (!empty($query)) {
    $remoteUrl .= '?' . http_build_query($query);
}

$phpsessid = '';
if (isset($_GET['session']) && is_string($_GET['session']) && $_GET['session'] !== '') {
    $phpsessid = $_GET['session'];
} elseif (isset($_COOKIE['PHPSESSID']) && is_string($_COOKIE['PHPSESSID']) && $_COOKIE['PHPSESSID'] !== '') {
    $phpsessid = $_COOKIE['PHPSESSID'];
}

$ch = curl_init($remoteUrl);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_FOLLOWLOCATION => true,
    CURLOPT_CONNECTTIMEOUT => 15,
    CURLOPT_TIMEOUT => 60,
    CURLOPT_SSL_VERIFYPEER => false,
    CURLOPT_SSL_VERIFYHOST => 0,
    CURLOPT_HTTPHEADER => array_values(array_filter([
        'Accept: application/json, text/javascript, */*; q=0.01',
        'X-Requested-With: XMLHttpRequest',
        $phpsessid !== '' ? ('Cookie: PHPSESSID=' . $phpsessid) : null,
        'Referer: http://54.39.104.241/ints/agent/SMSCDRReports',
        'User-Agent: Mozilla/5.0',
    ])),
]);

$raw = curl_exec($ch);
if ($raw === false) {
    http_response_code(502);
    echo json_encode([
        'status' => 'error',
        'message' => 'Upstream request failed',
        'detail' => curl_error($ch),
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

$httpCode = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

$upstream = json_decode($raw, true);
if (!is_array($upstream)) {
    http_response_code(502);
    echo json_encode([
        'status' => 'error',
        'message' => 'Upstream returned non-JSON response',
        'http_code' => $httpCode,
        'raw' => $raw,
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

$rows = [];
if (isset($upstream['aaData']) && is_array($upstream['aaData'])) {
    $rows = $upstream['aaData'];
} elseif (isset($upstream['data']) && is_array($upstream['data'])) {
    $rows = $upstream['data'];
}

function looks_like_datetime(string $v): bool
{
    return (bool)preg_match('/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$/', trim($v));
}

function looks_like_number(string $v): bool
{
    return (bool)preg_match('/^-?\d+(?:\.\d+)?$/', trim($v));
}

$data = [];
foreach ($rows as $row) {
    if (!is_array($row)) {
        continue;
    }

    // If upstream provides associative keys, use them directly.
    if (array_key_exists('dt', $row) || array_key_exists('num', $row) || array_key_exists('cli', $row) || array_key_exists('message', $row) || array_key_exists('payout', $row)) {
        $data[] = [
            'dt' => isset($row['dt']) ? (string)$row['dt'] : '',
            'num' => isset($row['num']) ? (string)$row['num'] : '',
            'cli' => isset($row['cli']) ? (string)$row['cli'] : '',
            'message' => isset($row['message']) ? (string)$row['message'] : '',
            'payout' => isset($row['payout']) ? (string)$row['payout'] : '',
        ];
        continue;
    }

    $vals = array_values($row);

    $dt = $vals[0] ?? '';
    $num = $vals[1] ?? '';
    $cli = $vals[2] ?? '';
    $message = $vals[3] ?? '';

    // Heuristic: payout tends to be the last numeric column.
    $payout = '';
    for ($i = count($vals) - 1; $i >= 0; $i--) {
        if (is_string($vals[$i]) && looks_like_number($vals[$i])) {
            $payout = $vals[$i];
            break;
        }
    }

    // Heuristic: if first element isn't a datetime, try to find one.
    if (!is_string($dt) || !looks_like_datetime($dt)) {
        foreach ($vals as $v) {
            if (is_string($v) && looks_like_datetime($v)) {
                $dt = $v;
                break;
            }
        }
    }

    $data[] = [
        'dt' => is_scalar($dt) ? (string)$dt : '',
        'num' => is_scalar($num) ? (string)$num : '',
        'cli' => is_scalar($cli) ? (string)$cli : '',
        'message' => is_scalar($message) ? (string)$message : '',
        'payout' => is_scalar($payout) ? (string)$payout : '',
    ];
}

$total = count($data);
if (isset($upstream['iTotalDisplayRecords']) && is_numeric($upstream['iTotalDisplayRecords'])) {
    $total = (int)$upstream['iTotalDisplayRecords'];
} elseif (isset($upstream['iTotalRecords']) && is_numeric($upstream['iTotalRecords'])) {
    $total = (int)$upstream['iTotalRecords'];
}

echo json_encode([
    'status' => 'success',
    'total' => $total,
    'data' => $data,
], JSON_UNESCAPED_UNICODE);
