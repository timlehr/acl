import argparse
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
import zipfile

# The current test data version in used
current_test_data = 'test_data_v1'
current_decomp_data = 'decomp_data_v2'

def parse_argv():
	parser = argparse.ArgumentParser(add_help=False)

	actions = parser.add_argument_group(title='Actions', description='If no action is specified, on Windows, OS X, and Linux the solution/make files are generated.  Multiple actions can be used simultaneously.')
	actions.add_argument('-build', action='store_true')
	actions.add_argument('-clean', action='store_true')
	actions.add_argument('-unit_test', action='store_true')
	actions.add_argument('-regression_test', action='store_true')

	target = parser.add_argument_group(title='Target')
	target.add_argument('-compiler', choices=['vs2015', 'vs2017', 'android', 'clang4', 'clang5', 'clang6', 'gcc5', 'gcc6', 'gcc7', 'gcc8', 'osx', 'ios'], help='Defaults to the host system\'s default compiler')
	target.add_argument('-config', choices=['Debug', 'Release'], type=str.capitalize)
	target.add_argument('-cpu', choices=['x86', 'x64'], help='Only supported for Windows, OS X, and Linux; defaults to the host system\'s architecture')

	misc = parser.add_argument_group(title='Miscellaneous')
	misc.add_argument('-avx', dest='use_avx', action='store_true', help='Compile using AVX instructions on Windows, OS X, and Linux')
	misc.add_argument('-pop', dest='use_popcnt', action='store_true', help='Compile using the POPCNT instruction')
	misc.add_argument('-nosimd', dest='use_simd', action='store_false', help='Compile without SIMD instructions')
	misc.add_argument('-num_threads', help='No. to use while compiling and regressing')
	misc.add_argument('-tests_matching', help='Only run tests whose names match this regex')
	misc.add_argument('-help', action='help', help='Display this usage information')

	parser.set_defaults(build=False, clean=False, unit_test=False, regression_test=False, compiler=None, config='Release', cpu='x64', use_avx=False, use_popcnt=False, use_simd=True, num_threads=4, tests_matching='')

	args = parser.parse_args()

	# Sanitize and validate our options
	if args.use_avx and not args.use_simd:
		print('SIMD is disabled; AVX cannot be used')
		args.use_avx = False

	if args.compiler == 'android':
		args.cpu = 'armv7-a'

		if not platform.system() == 'Windows':
			print('Android is only supported on Windows')
			sys.exit(1)

		if args.use_avx:
			print('AVX is not supported on Android')
			sys.exit(1)

		if args.unit_test:
			print('Unit tests cannot run from the command line on Android')
			sys.exit(1)

	if args.compiler == 'ios':
		args.cpu = 'arm64'

		if not platform.system() == 'Darwin':
			print('iOS is only supported on OS X')
			sys.exit(1)

		if args.use_avx:
			print('AVX is not supported on iOS')
			sys.exit(1)

		if args.unit_test:
			print('Unit tests cannot run from the command line on iOS')
			sys.exit(1)

	return args

def get_cmake_exes():
	if platform.system() == 'Windows':
		return ('cmake.exe', 'ctest.exe')
	else:
		return ('cmake', 'ctest')

def get_generator(compiler, cpu):
	if compiler == None:
		return None

	if platform.system() == 'Windows':
		if compiler == 'vs2015':
			if cpu == 'x86':
				return 'Visual Studio 14'
			else:
				return 'Visual Studio 14 Win64'
		elif compiler == 'vs2017':
			if cpu == 'x86':
				return 'Visual Studio 15'
			else:
				return 'Visual Studio 15 Win64'
		elif compiler == 'android':
			return 'Visual Studio 14'
	elif platform.system() == 'Darwin':
		if compiler == 'osx' or compiler == 'ios':
			return 'Xcode'
	else:
		return 'Unix Makefiles'

	print('Unknown compiler: {}'.format(compiler))
	print('See help with: python make.py -help')
	sys.exit(1)

def get_toolchain(compiler):
	if platform.system() == 'Windows' and compiler == 'android':
		return 'Toolchain-Android.cmake'
	elif platform.system() == 'Darwin' and compiler == 'ios':
		return 'Toolchain-iOS.cmake'

	# No toolchain
	return None

def set_compiler_env(compiler, args):
	if platform.system() == 'Linux':
		os.environ['MAKEFLAGS'] = '-j{}'.format(args.num_threads)
		if compiler == 'clang4':
			os.environ['CC'] = 'clang-4.0'
			os.environ['CXX'] = 'clang++-4.0'
		elif compiler == 'clang5':
			os.environ['CC'] = 'clang-5.0'
			os.environ['CXX'] = 'clang++-5.0'
		elif compiler == 'clang6':
			os.environ['CC'] = 'clang-6.0'
			os.environ['CXX'] = 'clang++-6.0'
		elif compiler == 'gcc5':
			os.environ['CC'] = 'gcc-5'
			os.environ['CXX'] = 'g++-5'
		elif compiler == 'gcc6':
			os.environ['CC'] = 'gcc-6'
			os.environ['CXX'] = 'g++-6'
		elif compiler == 'gcc7':
			os.environ['CC'] = 'gcc-7'
			os.environ['CXX'] = 'g++-7'
		elif compiler == 'gcc8':
			os.environ['CC'] = 'gcc-8'
			os.environ['CXX'] = 'g++-8'
		else:
			print('Unknown compiler: {}'.format(compiler))
			print('See help with: python make.py -help')
			sys.exit(1)

def do_generate_solution(cmake_exe, build_dir, cmake_script_dir, test_data_dir, decomp_data_dir, args):
	compiler = args.compiler
	cpu = args.cpu
	config = args.config

	if not compiler == None:
		set_compiler_env(compiler, args)

	extra_switches = ['--no-warn-unused-cli']
	if not platform.system() == 'Windows':
		extra_switches.append('-DCPU_INSTRUCTION_SET:STRING={}'.format(cpu))

	if args.use_avx:
		print('Enabling AVX usage')
		extra_switches.append('-DUSE_AVX_INSTRUCTIONS:BOOL=true')

	if args.use_popcnt:
		print('Enabling POPCOUNT usage')
		extra_switches.append('-DUSE_POPCNT_INSTRUCTIONS:BOOL=true')

	if not args.use_simd:
		print('Disabling SIMD instruction usage')
		extra_switches.append('-DUSE_SIMD_INSTRUCTIONS:BOOL=false')

	if not platform.system() == 'Windows' and not platform.system() == 'Darwin':
		extra_switches.append('-DCMAKE_BUILD_TYPE={}'.format(config.upper()))

	toolchain = get_toolchain(compiler)
	if not toolchain == None:
		extra_switches.append('-DCMAKE_TOOLCHAIN_FILE="{}"'.format(os.path.join(cmake_script_dir, toolchain)))

	if test_data_dir:
		extra_switches.append('-DTEST_DATA_DIR:STRING="{}"'.format(test_data_dir))

	if decomp_data_dir:
		extra_switches.append('-DDECOMP_DATA_DIR:STRING="{}"'.format(decomp_data_dir))

	# Generate IDE solution
	print('Generating build files ...')
	cmake_cmd = '"{}" .. -DCMAKE_INSTALL_PREFIX="{}" {}'.format(cmake_exe, build_dir, ' '.join(extra_switches))
	cmake_generator = get_generator(compiler, cpu)
	if cmake_generator == None:
		print('Using default generator')
	else:
		print('Using generator: {}'.format(cmake_generator))
		cmake_cmd += ' -G "{}"'.format(cmake_generator)

	result = subprocess.call(cmake_cmd, shell=True)
	if result != 0:
		sys.exit(result)

def do_build(cmake_exe, args):
	config = args.config

	print('Building ...')
	cmake_cmd = '"{}" --build .'.format(cmake_exe)
	if platform.system() == 'Windows':
		if args.compiler == 'android':
			cmake_cmd += ' --config {}'.format(config)
		else:
			cmake_cmd += ' --config {} --target INSTALL'.format(config)
	elif platform.system() == 'Darwin':
		if args.compiler == 'ios':
			cmake_cmd += ' --config {}'.format(config)
		else:
			cmake_cmd += ' --config {} --target install'.format(config)
	else:
		cmake_cmd += ' --target install'

	result = subprocess.call(cmake_cmd, shell=True)
	if result != 0:
		sys.exit(result)

def do_tests(ctest_exe, args):
	print('Running unit tests ...')

	ctest_cmd = '"{}" --output-on-failure'.format(ctest_exe)
	if platform.system() == 'Windows' or platform.system() == 'Darwin':
		ctest_cmd += ' -C {}'.format(args.config)
	if args.tests_matching:
		ctest_cmd += ' --tests-regex {}'.format(args.tests_matching)

	result = subprocess.call(ctest_cmd, shell=True)
	if result != 0:
		sys.exit(result)

def format_elapsed_time(elapsed_time):
	hours, rem = divmod(elapsed_time, 3600)
	minutes, seconds = divmod(rem, 60)
	return '{:0>2}h {:0>2}m {:05.2f}s'.format(int(hours), int(minutes), seconds)

def print_progress(iteration, total, prefix='', suffix='', decimals = 1, bar_length = 50):
	# Taken from https://stackoverflow.com/questions/3173320/text-progress-bar-in-the-console
	"""
	Call in a loop to create terminal progress bar
	@params:
		iteration   - Required  : current iteration (Int)
		total       - Required  : total iterations (Int)
		prefix      - Optional  : prefix string (Str)
		suffix      - Optional  : suffix string (Str)
		decimals    - Optional  : positive number of decimals in percent complete (Int)
		bar_length  - Optional  : character length of bar (Int)
	"""
	str_format = "{0:." + str(decimals) + "f}"
	percents = str_format.format(100 * (iteration / float(total)))
	filled_length = int(round(bar_length * iteration / float(total)))
	bar = '█' * filled_length + '-' * (bar_length - filled_length)

	if platform.system() == 'Darwin':
		# On OS X, \r doesn't appear to work properly in the terminal
		print('{}{} |{}| {}{} {}'.format('\b' * 100, prefix, bar, percents, '%', suffix), end='')
	else:
		sys.stdout.write('\r%s |%s| %s%s %s' % (prefix, bar, percents, '%', suffix)),

	if iteration == total:
		print('')

	sys.stdout.flush()

def do_prepare_regression_test_data(test_data_dir, args):
	print('Preparing regression test data ...')

	current_test_data_zip = os.path.join(test_data_dir, '{}.zip'.format(current_test_data))

	# Validate that our regression test data is present
	if not os.path.exists(current_test_data_zip):
		print('Regression test data not found: {}'.format(current_test_data_zip))
		return

	# If it hasn't been decompressed yet, do so now
	current_test_data_dir = os.path.join(test_data_dir, current_test_data)
	needs_decompression = not os.path.exists(current_test_data_dir)
	if needs_decompression:
		print('Decompressing {} ...'.format(current_test_data_zip))
		with zipfile.ZipFile(current_test_data_zip, 'r') as zip_ref:
			zip_ref.extractall(test_data_dir)

	# Grab all the test clips
	regression_clips = []
	for (dirpath, dirnames, filenames) in os.walk(current_test_data_dir):
		for filename in filenames:
			if not filename.endswith('.acl.sjson'):
				continue

			clip_filename = os.path.join(dirpath, filename)
			regression_clips.append((clip_filename, os.path.getsize(clip_filename)))

	if len(regression_clips) == 0:
		print('No regression clips found')
		sys.exit(1)

	print('Found {} regression clips'.format(len(regression_clips)))

	# Grab all the test configurations
	test_configs = []
	test_config_dir = os.path.join(test_data_dir, 'configs')
	if os.path.exists(test_config_dir):
		for (dirpath, dirnames, filenames) in os.walk(test_config_dir):
			for filename in filenames:
				if not filename.endswith('.config.sjson'):
					continue

				config_filename = os.path.join(dirpath, filename)
				test_configs.append((config_filename, filename))

	if len(test_configs) == 0:
		print('No regression configurations found')
		sys.exit(1)

	print('Found {} regression configurations'.format(len(test_configs)))

	if needs_decompression:
		# Sort the configs by name for consistency
		test_configs.sort(key=lambda entry: entry[1])

		# Sort clips by size to test larger clips first, it parallelizes better
		regression_clips.sort(key=lambda entry: entry[1], reverse=True)

		with open(os.path.join(current_test_data_dir, 'metadata.sjson'), 'w') as metadata_file:
			print('configs = [', file = metadata_file)
			for config_filename, _ in test_configs:
				print('\t"{}"'.format(os.path.relpath(config_filename, test_config_dir)), file = metadata_file)
			print(']', file = metadata_file)
			print('', file = metadata_file)
			print('clips = [', file = metadata_file)
			for clip_filename, _ in regression_clips:
				print('\t"{}"'.format(os.path.relpath(clip_filename, current_test_data_dir)), file = metadata_file)
			print(']', file = metadata_file)
			print('', file = metadata_file)

	return current_test_data_dir

def do_prepare_decompression_test_data(test_data_dir, args):
	print('Preparing decompression test data ...')

	current_data_zip = os.path.join(test_data_dir, '{}.zip'.format(current_decomp_data))

	# Validate that our regression test data is present
	if not os.path.exists(current_data_zip):
		print('Decompression test data not found: {}'.format(current_data_zip))
		return

	# If it hasn't been decompressed yet, do so now
	current_data_dir = os.path.join(test_data_dir, current_decomp_data)
	needs_decompression = not os.path.exists(current_data_dir)
	if needs_decompression:
		print('Decompressing {} ...'.format(current_data_zip))
		with zipfile.ZipFile(current_data_zip, 'r') as zip_ref:
			zip_ref.extractall(test_data_dir)

	# Grab all the test clips
	clips = []
	for (dirpath, dirnames, filenames) in os.walk(current_data_dir):
		for filename in filenames:
			if not filename.endswith('.acl.bin'):
				continue

			clip_filename = os.path.join(dirpath, filename)
			clips.append(clip_filename)

	if len(clips) == 0:
		print('No decompression clips found')
		sys.exit(1)

	print('Found {} decompression clips'.format(len(clips)))

	# Grab all the test configurations
	configs = []
	config_dir = os.path.join(test_data_dir, 'configs')
	if os.path.exists(config_dir):
		for (dirpath, dirnames, filenames) in os.walk(config_dir):
			for filename in filenames:
				if not filename.endswith('.config.sjson'):
					continue

				if not filename == 'uniformly_sampled_quant_var_2.config.sjson':
					continue

				config_filename = os.path.join(dirpath, filename)
				configs.append(config_filename)

	if len(configs) == 0:
		print('No decompression configurations found')
		sys.exit(1)

	print('Found {} decompression configurations'.format(len(configs)))

	if needs_decompression:
		with open(os.path.join(current_data_dir, 'metadata.sjson'), 'w') as metadata_file:
			print('configs = [', file = metadata_file)
			for config_filename in configs:
				print('\t"{}"'.format(os.path.relpath(config_filename, config_dir)), file = metadata_file)
			print(']', file = metadata_file)
			print('', file = metadata_file)
			print('clips = [', file = metadata_file)
			for clip_filename in clips:
				print('\t"{}"'.format(os.path.relpath(clip_filename, current_data_dir)), file = metadata_file)
			print(']', file = metadata_file)
			print('', file = metadata_file)

	return current_data_dir

def do_regression_tests(ctest_exe, test_data_dir, args):
	print('Running regression tests ...')

	# Validate that our regression testing tool is present
	if platform.system() == 'Windows':
		compressor_exe_path = './bin/acl_compressor.exe'
	else:
		compressor_exe_path = './bin/acl_compressor'

	compressor_exe_path = os.path.abspath(compressor_exe_path)
	if not os.path.exists(compressor_exe_path):
		print('Compressor exe not found: {}'.format(compressor_exe_path))
		sys.exit(1)

	# Grab all the test clips
	regression_clips = []
	current_test_data_dir = os.path.join(test_data_dir, current_test_data)
	for (dirpath, dirnames, filenames) in os.walk(current_test_data_dir):
		for filename in filenames:
			if not filename.endswith('.acl.sjson'):
				continue

			clip_filename = os.path.join(dirpath, filename)
			regression_clips.append((clip_filename, os.path.getsize(clip_filename)))

	# Grab all the test configurations
	test_configs = []
	test_config_dir = os.path.join(test_data_dir, 'configs')
	if os.path.exists(test_config_dir):
		for (dirpath, dirnames, filenames) in os.walk(test_config_dir):
			for filename in filenames:
				if not filename.endswith('.config.sjson'):
					continue

				config_filename = os.path.join(dirpath, filename)
				test_configs.append((config_filename, filename))

	# Sort the configs by name for consistency
	test_configs.sort(key=lambda entry: entry[1])

	# Sort clips by size to test larger clips first, it parallelizes better
	regression_clips.sort(key=lambda entry: entry[1], reverse=True)

	# Iterate over every clip and configuration and perform the regression testing
	for config_filename, _ in test_configs:
		print('Performing regression tests for configuration: {}'.format(os.path.basename(config_filename)))
		regression_start_time = time.clock()

		cmd_queue = queue.Queue()
		completed_queue = queue.Queue()
		failed_queue = queue.Queue()
		failure_lock = threading.Lock()
		for clip_filename, _ in regression_clips:
			cmd = '{} -acl="{}" -test -config="{}"'.format(compressor_exe_path, clip_filename, config_filename)
			if platform.system() == 'Windows':
				cmd = cmd.replace('/', '\\')

			cmd_queue.put((clip_filename, cmd))

		# Add a marker to terminate the threads
		for i in range(args.num_threads):
			cmd_queue.put(None)

		def run_clip_regression_test(cmd_queue, completed_queue, failed_queue, failure_lock):
			while True:
				entry = cmd_queue.get()
				if entry is None:
					return

				(clip_filename, cmd) = entry

				result = os.system(cmd)

				if result != 0:
					failed_queue.put((clip_filename, cmd))
					failure_lock.acquire()
					print('Failed to run regression test for clip: {}'.format(clip_filename))
					print(cmd)
					failure_lock.release()

				completed_queue.put(clip_filename)

		threads = [ threading.Thread(target = run_clip_regression_test, args = (cmd_queue, completed_queue, failed_queue, failure_lock)) for _i in range(args.num_threads) ]
		for thread in threads:
			thread.daemon = True
			thread.start()

		print_progress(0, len(regression_clips), 'Testing clips:', '{} / {}'.format(0, len(regression_clips)))
		try:
			while True:
				for thread in threads:
					thread.join(1.0)

				num_processed = completed_queue.qsize()
				print_progress(num_processed, len(regression_clips), 'Testing clips:', '{} / {}'.format(num_processed, len(regression_clips)))

				all_threads_done = True
				for thread in threads:
					if thread.isAlive():
						all_threads_done = False

				if all_threads_done:
					break
		except KeyboardInterrupt:
			sys.exit(1)

		regression_testing_failed = not failed_queue.empty()

		regression_end_time = time.clock()
		print('Done in {}'.format(format_elapsed_time(regression_end_time - regression_start_time)))

		if regression_testing_failed:
			sys.exit(1)

if __name__ == "__main__":
	args = parse_argv()

	cmake_exe, ctest_exe = get_cmake_exes()

	# Set the ACL_CMAKE_HOME environment variable to point to CMake
	# otherwise we assume it is already in the user PATH
	if 'ACL_CMAKE_HOME' in os.environ:
		cmake_home = os.environ['ACL_CMAKE_HOME']
		cmake_exe = os.path.join(cmake_home, 'bin', cmake_exe)
		ctest_exe = os.path.join(cmake_home, 'bin', ctest_exe)

	build_dir = os.path.join(os.getcwd(), 'build')
	test_data_dir = os.path.join(os.getcwd(), 'test_data')
	cmake_script_dir = os.path.join(os.getcwd(), 'cmake')

	if args.clean and os.path.exists(build_dir):
		print('Cleaning previous build ...')
		shutil.rmtree(build_dir)

	if not os.path.exists(build_dir):
		os.makedirs(build_dir)

	os.chdir(build_dir)

	print('Using config: {}'.format(args.config))
	print('Using cpu: {}'.format(args.cpu))
	if not args.compiler == None:
		print('Using compiler: {}'.format(args.compiler))

	regression_data_dir = do_prepare_regression_test_data(test_data_dir, args)
	decomp_data_dir = do_prepare_decompression_test_data(test_data_dir, args)

	do_generate_solution(cmake_exe, build_dir, cmake_script_dir, regression_data_dir, decomp_data_dir, args)

	if args.build:
		do_build(cmake_exe, args)

	if args.unit_test:
		do_tests(ctest_exe, args)

	if args.regression_test and not args.compiler == 'android' and not args.compiler == 'ios':
		do_regression_tests(ctest_exe, test_data_dir, args)

	sys.exit(0)
